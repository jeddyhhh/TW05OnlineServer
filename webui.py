"""The web site for the TW05 master server.  Standard library only.

    python webui.py --db ../run/tw05.db --port 8081

TW05's copy of TW04's site (tools/webui.py): the same pages, drawn in the
TW05 menus' colours -- teal, maroon and gold -- and taught TW05's game types
(the 3 Hole Mini-Game and Battle) and its online cash.  The download is built
per server like TW04's: the DNAS bypass, and this server's address written
over the host names TW05 looks up (HOST_SLOTS).

No framework, no template engine, no static files -- one process next to
`lobbyd`, sharing its sqlite file.  Accounts made here are what the console's
login screen accepts, and the personas this site manages are the list the
game offers on the SELECT EA ACCOUNT screen.
"""
import argparse
import collections
import datetime
import html
import http.cookies
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twdb
import twlog
import twrecords
import twstats
import twtourney

DB = None
# One line per request, capped -- see twlog.  stdout only until main() runs.
LOG = twlog.Log(echo=True)
DEFAULT_LOG = os.path.join(os.path.dirname(os.path.dirname(twdb.DEFAULT_DB)),
                           'logs', 'webui.log')
BASE = ''                     # mounted under this path, e.g. '/TW05Online'
TRUST_PROXY = False           # read X-Forwarded-For?  Only behind one.
SECURE_COOKIE = False         # add `Secure`; on when there is TLS in front
COOKIE = 'tw05'               # not TW04's, so the two sites can share a host

# What to tell a visitor to point their game at.  TW05 looks the lobby up by
# name, and PCSX2's DNS override answers that name with an IPv4 address, so
# whatever is set here has to end up as one.  Empty means "work it out from
# the address this request arrived at", which is right whenever the lobby and
# the web site are the same machine, and wrong otherwise; that is what
# --advertise is for.
ADVERTISE = ''
LOBBY_PORT = 20200            # compiled into the disc beside the name

# The names TW05 looks up, all of which have to come here.  The lobby is the
# one that matters; Messenger is the buddy list; the demangler is EA's NAT
# helper at match start, and its name now belongs to somebody else entirely,
# so it is pointed here to keep the game from talking to them (a match falls
# back to connecting directly after about 20 seconds either way).
#
# Where those names live in the ELF (vaddr, bytes the slot has, the name).
# The patch writes this server's address over each one as a dotted quad,
# which the game takes as it is -- no DNS, so nothing to set in PCSX2
# (Jed, 2026-09-29: works with the host overrides removed).  The lobby's
# slot is 16 bytes, room for the longest IPv4 address and its NUL.
# ps2tw05.ea.com is in the ELF twice: Lobby_Init (0x001C3350) reads the
# first, the demo/attract path (0x001CC56C) the second.
HOST_SLOTS = ((0x00361208, 16, 'ps2tw05.ea.com'),
              (0x00363D38, 16, 'ps2tw05.ea.com'),
              (0x0035F0F0, 20, 'msgconn.beta.ea.com'),
              (0x003655D8, 20, 'demangler.ea.com'))

SERIAL, CRC = 'SLUS-21002', '88A808FA'
PNACH_NAME = '%s_%s.pnach' % (SERIAL, CRC)
PNACH = """gametitle=Tiger Woods PGA Tour 2005 (USA) [%(serial)s]
comment=Online revival: master server -> %(ip)s. Downloaded from this server's web site.

// --- DNAS: treat the finished DNAS run as a pass ---
// 0x001BDDF4 loads the DNAS result (gDNASOutputBlock.iResult); zero is success.
patch=1,EE,001BDDF4,word,00002021

// --- The master server: EA's host names replaced with %(ip)s ---
%(hosts)s"""


DNAS_PATCH = (0x001BDDF4, 0x00002021)


def host_writes(ip):
    """[(name, [(vaddr, word), ...])]: `ip` over every host name in
    HOST_SLOTS, NUL-padded to the slot, as little-endian words."""
    raw = ip.encode('ascii')
    out = []
    for vaddr, size, name in HOST_SLOTS:
        if len(raw) >= size:
            raise ValueError('%s does not fit in %d bytes' % (ip, size))
        new = raw.ljust(size, b'\0')
        out.append((name, [(vaddr + k, int.from_bytes(new[k:k + 4], 'little'))
                           for k in range(0, size, 4)]))
    return out


def host_patches(ip):
    """pnach lines writing `ip` over every host name in HOST_SLOTS."""
    lines = []
    for name, words in host_writes(ip):
        lines.append('// %s' % name)
        lines.extend('patch=1,EE,%08X,word,%08X' % w for w in words)
    return '\n'.join(lines) + '\n'


# The same patch for a real console's cheat engine (Open PS2 Loader's, or
# Cheat Device), which only runs codes once it has hooked the game.  The
# hook is the "9" master code: a `jal` the game makes every frame, and the
# instruction there.  TW04's was its CodeBreaker master code decrypted -- a
# `jal memcpy` inside libpad's scePadRead.  TW05's CodeBreaker master code is
# in the v7 encryption, so this one was found the other way round: the same
# `jal memcpy` in TW05's scePadRead (libpad 2800, 0x00312E58; called from the
# game's pad update at 0x002D48F4), confirmed in PCSX2's debugger to fire
# once a frame (2026-09-29).  Every other code is a type-2 32-bit write of
# exactly what the .pnach writes.  NOT TRIED ON A REAL CONSOLE.
MASTER_CODE = (0x00312F7C, 0x0C0BBF8A)            # jal 0x002EFE28 (memcpy)
CHT_NAME = 'SLUS_210.02.cht'                      # OPL looks for <game ID>.cht
CHEATDEVICE_NAME = 'TW05-CheatDevice.txt'
REAL_PS2_TITLE = 'Tiger Woods PGA Tour 2005 (NTSC-U)'


def cheat_codes(ip):
    """(master lines, online lines): a name, then its codes."""
    master = ['Master Code', '9%07X %08X' % MASTER_CODE]
    online = ['TW05 Online - UNTESTED (server %s)' % ip,
              '2%07X %08X' % DNAS_PATCH]
    online += ['2%07X %08X' % w for _name, words in host_writes(ip)
               for w in words]
    return master, online


def build_cht(ip):
    """Open PS2 Loader's <game ID>.cht (PS2rd format).  Every line that is
    not 16 hex digits is read as a cheat NAME, so it carries no comments."""
    master, online = cheat_codes(ip)
    return '\n'.join(master + [''] + online) + '\n'


def build_cheatdevice(ip):
    """Cheat Device's TXT database: the game title in quotes, then cheats."""
    master, online = cheat_codes(ip)
    return '\n'.join(['"%s"' % REAL_PS2_TITLE] + master + [''] + online) + '\n'

# Starting cash when lobbyd has not published its --start-cash.
START_CASH = twrecords.START_CASH

# The operator's abuse-reports page lives at /reports/<REPORTS_KEY> and nowhere
# else: no link to it, no login, and any other path under /reports is an
# ordinary 404, so its existence is not advertised.  The key is made once and
# kept beside the database (see `reports_key`).  Empty switches the page off.
REPORTS_KEY = ''

# The operator's admin page -- password resets, renames, bans and the in-game
# news -- at /admin/<ADMIN_KEY>, kept exactly like the reports page: made once
# beside the database, never linked.  Empty switches it off.
ADMIN_KEY = ''
# The in-game news file lobbyd reads (its --news).  Both default to news.txt
# beside the database.
NEWS_FILE = ''
NEWS_LIMIT = 4000             # lobbyd's buffer is 4999, and the digest follows
_ENDPOINT = [0.0, None]       # (when it was resolved, (ip, port)) -- see below
ENDPOINT_TTL = 300


SESSIONS = {}                 # token -> {'account': id, 'csrf': str, 'seen': ts}
SESSION_LOCK = threading.Lock()
SESSION_MAX_AGE = 12 * 3600
ATTEMPTS = {}                 # client ip -> [count, first attempt]
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 300

CSS = """
/* The TW05 menus: a deep teal sea behind dark glass panels, each panel headed
   by a maroon bar, titles in gold, the chosen row a pale grey pill.  Colours
   sampled off the game's own SELECT EA ACCOUNT screen. */
:root {
  --sea:#13262b; --sea-2:#1c363d; --sea-3:#285058;
  --panel:rgba(8,18,20,.84); --panel-2:rgba(0,0,0,.26);
  --line:#26434a; --line-2:#385a62;
  --ink:#eef3f3; --mute:#9db5ba; --dim:#6f8b91;
  --gold:#e0a800; --gold-2:#f3c434;
  --maroon:#480000; --maroon-2:#6e1418; --maroon-3:#8c2a30;
  --pill:#c9cfcf; --pill-ink:#10191b;
  --link:#a9dbe4; --under:#8fe0ae; --over:#f2a092;
  --c-match:#4fa6b3; --c-peak:#a9dbe4;
  color-scheme: dark;
}
* { box-sizing:border-box; }
html { background:var(--sea); }
body { margin:0; color:var(--ink); min-height:100vh;
       font:16px/1.6 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
       background:radial-gradient(ellipse 120% 80% at 50% 0%,
                    #33606a 0%, #214149 38%, #152b31 70%, #0f2126 100%)
                  fixed; }
/* the two great faint rings the menus are drawn over */
body::before, body::after { content:""; position:fixed; z-index:-1;
  pointer-events:none; border-radius:50%; }
body::before { left:-25vw; right:-25vw; top:12vh; height:150vh;
  border:2px solid rgba(255,255,255,.07);
  box-shadow:0 0 0 28px rgba(255,255,255,.025); }
body::after { left:8vw; right:8vw; top:26vh; height:120vh;
  border:1px solid rgba(255,255,255,.05); }

/* the menus' heavy slanted capitals */
.brand, h1, h2, nav, button, a.dl, .strip, th, label, .figure span, .tag,
.feed .k { font-style:italic; }

/* header */
.top { background:linear-gradient(180deg,rgba(6,14,16,.92),rgba(12,28,32,.78));
       border-bottom:2px solid rgba(214,226,228,.55);
       box-shadow:0 1px 0 rgba(0,0,0,.5); }
.top .wrap { display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
             padding:1rem 1rem .9rem; }
.brand { font-size:1.15rem; font-weight:800; letter-spacing:.06em;
         text-transform:uppercase; margin:0; color:var(--ink);
         text-shadow:0 2px 0 rgba(0,0,0,.45); }
.brand span { color:var(--gold); }
.brand small { display:block; font-size:.68rem; letter-spacing:.2em;
               color:var(--mute); font-weight:700; margin-top:.1rem;
               text-shadow:none; }
/* The menu is a row of its own under the title, starting at the left, on
   one line (signed in there are nine links).  The negative margins take back
   the links' own padding, so the first link's text lines up with the title
   and the last one's with the page. */
nav { flex:1 0 100%; display:flex; gap:.3rem; flex-wrap:nowrap;
      overflow-x:auto; scrollbar-width:none; margin:0 -.7rem;
      font-size:.8rem; font-weight:700; letter-spacing:.06em;
      text-transform:uppercase; }
nav::-webkit-scrollbar { display:none; }
nav a { color:var(--ink); text-decoration:none; padding:.2rem .7rem;
        border-radius:999px; white-space:nowrap; }
nav a:hover { background:var(--pill); color:var(--pill-ink); }

/* On a phone the row gives way to a MENU button -- white capitals on a
   maroon bar, like a panel's title -- that drops the links down over the
   page, one to a line, the one under your finger in the pale pill the menus
   mark the chosen row with.  Below 900px, where the row would otherwise have
   to scroll. */
.top { position:relative; }
.menu { display:none; }
.menu summary { list-style:none; cursor:pointer; display:flex; align-items:center;
  gap:.55rem; padding:.45rem .85rem; color:#fff; font-size:.8rem;
  font-weight:800; font-style:italic; letter-spacing:.06em;
  text-transform:uppercase; user-select:none;
  background:linear-gradient(90deg,var(--maroon-2),var(--maroon));
  border-left:4px solid var(--maroon-3); }
.menu summary::-webkit-details-marker { display:none; }
.menu summary::before { content:""; width:1.1rem; height:2px;
  background:currentColor; box-shadow:0 -5px 0 currentColor, 0 5px 0 currentColor;
  margin:5px 0; }
.menu summary:focus-visible { outline:2px solid var(--gold); outline-offset:2px; }
.menu[open] summary { background:var(--pill); color:var(--pill-ink);
  border-left-color:var(--gold); }
.menu-list { position:absolute; left:0; right:0; top:100%; z-index:20;
  background:#0a1416; border-bottom:2px solid rgba(214,226,228,.55);
  box-shadow:0 12px 24px rgba(0,0,0,.55); padding:.5rem .6rem .7rem; }
.menu-list a { display:block; margin:.1rem 0; padding:.65rem 1rem;
  border-radius:999px; color:var(--ink); text-decoration:none;
  font-size:.95rem; font-weight:800; font-style:italic; letter-spacing:.05em;
  text-transform:uppercase; }
.menu-list a:hover, .menu-list a:focus-visible { background:var(--pill);
  color:var(--pill-ink); outline:none; }
@media (max-width:900px) {
  nav { display:none; }
  .menu { display:block; margin-left:auto; }
  .top .wrap { flex-wrap:nowrap; }
}
/* Beside the button on a phone the title gets a little smaller, to stay on
   one line; and "PGA Tour 2005" never breaks in the middle -- on a phone too
   narrow even for that, the title wraps after "Tiger Woods". */
.brand span { white-space:nowrap; }
@media (max-width:480px) {
  .brand { font-size:.9rem; letter-spacing:.03em; }
  .brand small { font-size:.6rem; letter-spacing:.12em; }
  .menu summary { padding:.4rem .65rem; gap:.45rem; }
}

.wrap { max-width:56rem; margin:0 auto; padding:0 1rem; }
main.wrap { padding-top:2rem; padding-bottom:1rem; }

h1 { font-size:1.75rem; margin:0 0 .3rem; font-weight:800; letter-spacing:.02em;
     color:var(--gold); text-shadow:0 2px 0 rgba(0,0,0,.5); line-height:1.25; }
h1 .foot, h1 a.pl { text-shadow:none; }
h1 a.pl { color:var(--gold); border-bottom-color:rgba(224,168,0,.45); }
/* a panel's title: white capitals on a maroon bar that fades out to the
   right, as on every TW05 menu panel */
h2 { font-size:.82rem; margin:0 0 1rem; color:#fff; font-weight:800;
     text-transform:uppercase; letter-spacing:.08em; padding:.42rem .9rem;
     background:linear-gradient(90deg,var(--maroon-2) 0%,var(--maroon) 55%,
                                rgba(72,0,0,0) 100%);
     border-left:4px solid var(--maroon-3); }
.card > h2:first-child { margin:-1.3rem -1.4rem 1.1rem; padding:.55rem 1.4rem; }
h3 { font-size:.95rem; margin:1.6rem 0 .5rem; color:var(--gold-2);
     font-style:italic; }
.sub { color:var(--mute); margin:0 0 1.8rem; }

/* the panels: square at the top, swept round at the bottom left, with the
   pale lip the menus have along their foot */
.card { background:var(--panel); border:1px solid rgba(255,255,255,.07);
        border-radius:2px 2px 2px 22px; padding:1.3rem 1.4rem 1.4rem;
        margin-bottom:1.2rem; overflow:hidden;
        box-shadow:inset 0 -3px 0 rgba(201,207,207,.28),
                   0 10px 30px rgba(0,0,0,.25); }
.cols { display:grid; gap:1.1rem; grid-template-columns:repeat(auto-fit,minmax(17rem,1fr)); }

/* tables */
table { width:100%; border-collapse:collapse; font-size:.9rem; }
caption { text-align:left; color:var(--mute); font-size:.8rem;
          padding-bottom:.5rem; }
th { text-align:left; padding:.4rem .4rem; color:var(--mute); font-weight:700;
     font-size:.72rem; text-transform:uppercase; letter-spacing:.08em;
     border-bottom:1px solid var(--line-2); }
td { padding:.5rem .4rem; border-bottom:1px solid var(--line); }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover td { background:rgba(255,255,255,.05); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.num, th.num { white-space:nowrap; }
tr.me td { background:rgba(224,168,0,.08); }
tr.me td:first-child { box-shadow:inset 3px 0 0 var(--gold); }
.rank { color:var(--mute); font-variant-numeric:tabular-nums; }
.rank-1 { color:var(--gold); font-weight:800; }
.under { color:var(--under); } .over { color:var(--over); }

/* the stats board */
.figures { display:grid; gap:.8rem;
           grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr)); }
.figure { background:var(--panel-2); border:1px solid var(--line);
          border-top:2px solid var(--maroon-3);
          border-radius:2px 2px 2px 12px; padding:.75rem .9rem; }
.figure b { display:block; font-size:1.5rem; line-height:1.15;
            font-variant-numeric:tabular-nums; }
.figure b small { font-size:.9rem; color:var(--mute); }
.figure span { display:block; font-size:.7rem; color:var(--mute);
               text-transform:uppercase; letter-spacing:.08em; margin-top:.25rem;
               font-weight:600; }
.figure b .dot { display:inline-block; margin-top:0; vertical-align:middle; }
footer .figure { background:rgba(8,18,20,.6); }
footer h2 { display:inline-block; padding-right:3rem; }

/* forms */
label { display:block; font-size:.76rem; color:var(--mute); margin:.8rem 0 .25rem;
        text-transform:uppercase; letter-spacing:.06em; font-weight:600; }
input[type=text], input[type=password], input[type=email], select {
  width:100%; padding:.6rem .75rem; border-radius:6px;
  border:1px solid var(--line-2); background:rgba(4,12,14,.8); color:var(--ink);
  font-size:1rem; }
input:focus, select:focus { outline:2px solid var(--gold); outline-offset:1px; }
button { margin-top:1.2rem; padding:.55rem 1.4rem; border:0; border-radius:999px;
         background:var(--gold); color:#1a1200; font-size:.86rem; font-weight:800;
         letter-spacing:.06em; text-transform:uppercase; cursor:pointer;
         box-shadow:0 2px 0 rgba(0,0,0,.4); }
button:hover { background:var(--gold-2); }
/* the pale pill the menus mark the chosen row with */
button.quiet { background:var(--pill); color:var(--pill-ink); }
button.quiet:hover { background:#e4e8e8; }
.row { display:flex; gap:1rem; flex-wrap:wrap; }
.row > * { flex:1 1 12rem; }

.msg { padding:.8rem 1rem; border-radius:2px 2px 2px 12px; margin:1.5rem 0 0;
       font-size:.9rem; }
.err { background:rgba(72,0,0,.55); border:1px solid var(--maroon-3);
       color:#ffd2d7; }
.ok  { background:rgba(20,70,60,.55); border:1px solid #2f7f6a; color:#c4f1e2; }

.pill { display:inline-block; background:var(--panel-2); border:1px solid var(--line-2);
        border-radius:999px; padding:.2rem .8rem; margin:0 .35rem .35rem 0;
        font-size:.85rem; }
a { color:var(--link); }
.foot { color:var(--dim); font-size:.8rem; }
footer { border-top:2px solid rgba(214,226,228,.25); margin-top:2rem;
         padding:1.5rem 0 2.5rem; background:rgba(6,14,16,.45); }
form.inline { display:inline; }
form.inline button { margin:0; padding:.2rem .8rem; font-size:.72rem; }

/* the patch download and the step lists */
a.dl { display:inline-block; background:var(--gold); color:#1a1200;
       text-decoration:none; border-radius:999px; padding:.55rem 1.3rem;
       font-size:.85rem; font-weight:800; letter-spacing:.05em;
       text-transform:uppercase; box-shadow:0 2px 0 rgba(0,0,0,.4); }
a.dl:hover { background:var(--gold-2); }
ol.steps { margin:.2rem 0 1.4rem; padding-left:1.3rem; font-size:.9rem;
           color:#d3e0e2; }
ol.steps li { margin:.45rem 0; }
ol.steps code, p code, .foot code, td code { background:rgba(4,12,14,.8);
       border:1px solid var(--line-2); border-radius:5px; padding:.05rem .35rem;
       font-size:.85em; }

/* the live strip and the live page */
.strip { background:rgba(4,10,12,.7); border-bottom:1px solid var(--line);
         font-size:.76rem; letter-spacing:.06em; text-transform:uppercase;
         color:var(--mute); font-weight:600; }
.strip .wrap { display:flex; gap:1.2rem; flex-wrap:wrap; align-items:center;
               padding:.5rem 1rem; }
.strip a { color:var(--mute); text-decoration:none; margin-left:auto; }
.strip a:hover { color:var(--gold); }
.dot { display:inline-block; width:.55rem; height:.55rem; border-radius:50%;
       margin-right:.45rem; vertical-align:baseline; background:var(--mute); }
.dot.up { background:#4fd18a; box-shadow:0 0 0 3px rgba(79,209,138,.2); }
.dot.down { background:#d8414f; box-shadow:0 0 0 3px rgba(216,65,79,.22); }
.dot.warm { background:var(--gold); }
.feed { list-style:none; margin:0; padding:0; font-size:.9rem; }
.feed li { display:flex; gap:.8rem; padding:.45rem 0;
           border-bottom:1px solid var(--line); }
.feed li:last-child { border-bottom:0; }
.feed time { color:var(--dim); font-size:.78rem; white-space:nowrap;
             min-width:5.5rem; font-variant-numeric:tabular-nums; }
.feed .k { color:var(--gold); font-size:.68rem; letter-spacing:.1em;
           text-transform:uppercase; min-width:5rem; font-weight:700; }
.tag { display:inline-block; font-size:.68rem; letter-spacing:.08em;
       text-transform:uppercase; border-radius:999px; padding:.05rem .6rem;
       border:1px solid var(--line-2); color:var(--mute); font-weight:700;
       vertical-align:middle; text-shadow:none; }
.tag.playing { border-color:#2f7f6a; color:#9be8cd; }
.tag.bad { border-color:var(--maroon-3); color:#f7a9b1; }
/* a player's or a course's name, linking to its page */
a.pl { color:inherit; text-decoration:none; border-bottom:1px dotted var(--line-2); }
a.pl:hover { color:var(--gold); border-bottom-color:var(--gold); }
.statgrid { display:grid; gap:1.1rem;
            grid-template-columns:repeat(auto-fit,minmax(19rem,1fr)); }
.statgrid .card { margin:0; }
.statgrid table { font-size:.86rem; }
.scroll { overflow-x:auto; }
.show-sm { display:none; }
@media (max-width:560px) { .hide-sm { display:none; } .show-sm { display:block; }
  .card { padding:1.1rem 1rem 1.2rem; }
  .card > h2:first-child { margin:-1.1rem -1rem 1rem; padding:.5rem 1rem; } }
.conds { color:var(--mute); font-size:.85rem; }
/* an event's prize money: hidden under its schedule row until the event's
   name is clicked (a #pay-<day> link), so it needs no script */
tr.payout { display:none; scroll-margin-top:5rem; }
tr.payout:target { display:table-row; }
/* one padding for the outer cell whether hovered or not -- a hover rule that
   out-ranked the inner cells' padding made the table jump */
tr.payout > td, tbody tr.payout:hover > td { background:rgba(0,0,0,.3);
  padding:.8rem 1rem; }
tr.payout table { max-width:24rem; font-size:.85rem; }
tr.payout table td { padding:.3rem .4rem; }
tr.payout caption a { float:right; }
.conds b { color:var(--ink); font-weight:600; }
/* achievements on a player page: earned ones lit, the rest dimmed */
.ach { display:grid; gap:.8rem; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr)); }
.ach div { background:var(--panel-2); border:1px solid var(--line);
           border-radius:2px 2px 2px 12px; padding:.8rem .9rem; }
.ach b { display:block; font-size:.95rem; font-style:italic; }
.ach span { display:block; font-size:.78rem; color:var(--mute); margin-top:.2rem; }
.ach .got { border-color:#7a6212; background:rgba(224,168,0,.07); }
.ach .got b { color:var(--gold); }
.ach .no { opacity:.5; }

/* the activity chart: one column per day, bars scaled to the busiest day */
.chart { display:flex; align-items:flex-end; gap:2px; height:130px;
         border-bottom:1px solid var(--line-2); }
.chart div { flex:1 1 0; display:flex; flex-direction:column-reverse;
             height:100%; min-width:0; }
.chart i { display:block; width:100%; }
.chart .r { background:var(--gold); }
.chart .m { background:var(--c-match); }
.chart .p { background:var(--c-peak); }
.chart .today i { opacity:.7; }
.axis { display:flex; justify-content:space-between; font-size:.72rem;
        color:var(--dim); margin-top:.3rem; }
.key { font-size:.78rem; color:var(--mute); margin:0 0 .6rem; }
.key i { display:inline-block; width:.7rem; height:.7rem; border-radius:2px;
         margin:0 .3rem 0 .8rem; vertical-align:-1px; }
.key i:first-child { margin-left:0; }

/* the comparison: the better of the two figures stands out */
td.better { color:var(--gold); font-weight:800; }
.vs { display:flex; gap:.6rem; align-items:flex-end; flex-wrap:wrap; }
.vs > div { flex:1 1 10rem; }
.vs button { margin:0; }

/* money: won in green, lost in red, as on the scoreboard */
.plus { color:var(--under); } .minus { color:var(--over); }

/* the admin page */
textarea { width:100%; min-height:14rem; padding:.6rem .75rem; border-radius:6px;
           border:1px solid var(--line-2); background:rgba(4,12,14,.8);
           color:var(--ink); font:.9rem/1.5 ui-monospace,Consolas,monospace; }
.admin-acct { border-top:1px solid var(--line); padding:1rem 0 .4rem; }
.admin-acct:first-of-type { border-top:0; padding-top:0; }
.admin-acct form { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
                   margin:.5rem 0 0; }
.admin-acct form input[type=text] { width:auto; flex:1 1 9rem; max-width:16rem;
                                    padding:.35rem .6rem; font-size:.9rem; }
.admin-acct form button { margin:0; padding:.4rem .9rem; font-size:.72rem; }
.admin-acct form .lbl { font-size:.8rem; color:var(--mute); min-width:8rem; }
.admin-acct form button.danger { background:var(--maroon-3); color:#fff; }

.chat { list-style:none; margin:.8rem 0 0; padding:0; font-size:.86rem; }
.chat li { padding:.3rem 0; border-bottom:1px solid var(--line); display:flex;
           gap:.7rem; align-items:baseline; }
.chat li:last-child { border-bottom:0; }
.chat time { color:var(--dim); font-size:.76rem; white-space:nowrap;
             font-variant-numeric:tabular-nums; }
.chat .who { color:var(--gold); white-space:nowrap; }
.chat .said { word-break:break-word; }
"""



def u(path='/'):
    """A path with the mount prefix on it.

    `page()` rewrites the links inside the HTML, but a redirect is a Location
    HEADER and never passes through it -- so redirects have to ask for the
    prefix themselves or they send the browser out of the mount point.
    """
    return (BASE + path) if path.startswith('/') else path


def _topar(text):
    """Colour a score relative to par, the way a leaderboard does."""
    cls = 'under' if text.startswith('-') else 'over' if text.startswith('+') else ''
    return '<span class="%s">%s</span>' % (cls, text) if cls else text


def plink(name):
    """A player's name, linking to their page."""
    return '<a class="pl" href="/player/%s">%s</a>' % (
        urllib.parse.quote(name, safe=''), html.escape(name))


def clink(course, kind=None):
    """A course's name, linking to its page.  The 3 Hole Mini-Game plays
    three random holes from random courses, so it has none."""
    if kind == 'mini':
        return '<span class="foot">3 random holes</span>'
    if course is None:
        return '<span class="foot">unknown course</span>'
    return '<a class="pl" href="/course/%d">%s</a>' % (
        course, html.escape(twrecords.course_name(course)))


def conditions_line(conditions):
    """Every tournament setting on one line -- 'Tees Black · Rough Long ...'
    -- so nobody has to know what the game's defaults are."""
    return '<span class="conds">%s</span>' % ' &middot; '.join(
        '%s <b>%s</b>' % (label, html.escape(option))
        for label, option, _default in twtourney.condition_items(conditions))


def conditions_cells(conditions, cls='hide-sm'):
    """The same settings as four table cells, Tees / Rough / Fairways /
    Greens, for the schedule."""
    return ''.join('<td class="%s">%s</td>' % (cls, html.escape(option))
                   for _label, option, _default in
                   twtourney.condition_items(conditions))


def _date(t):
    return time.strftime('%d %b %Y', time.localtime(t)) if t else ''


def new_password():
    """Eight letters and digits, none that look alike -- something a player
    can read off a screen and type on the console's keyboard."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    n = max(twdb.MIN_PASSWORD, min(8, twdb.MAX_PASSWORD))
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _golfer_list(rs):
    """A <datalist> of everyone with a finished round, so the compare boxes
    suggest names as they are typed -- no script needed."""
    names = sorted({r['persona'] for r in rs}, key=str.lower)
    return '<datalist id="golfers">%s</datalist>' % ''.join(
        '<option value="%s">' % html.escape(n, quote=True) for n in names)


def event_href(day):
    """/event/2026-09-24 -- an event's page, by its date."""
    return '/event/%s' % twtourney.from_day(day).isoformat()


def h2h_href(a, b):
    return '/h2h/%s/%s' % (urllib.parse.quote(a, safe=''),
                           urllib.parse.quote(b, safe=''))


def closes_in(now=None):
    """'6h 12m' until the server's midnight, when today's event closes."""
    now = now or datetime.datetime.now()
    midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1),
                                         datetime.time.min)
    mins = max(1, int((midnight - now).total_seconds() // 60))
    return '%dh %02dm' % divmod(mins, 60) if mins >= 60 else '%dm' % mins


def server_tz(now=None):
    """The server's timezone, which is when the tournament day turns over:
    'AEST, UTC+10' where the zone has a short name, else just 'UTC+10'.
    (Windows names zones in full -- 'AUS Eastern Standard Time' -- which is
    too long to be useful in a heading.)"""
    t = time.localtime(now)
    off = t.tm_gmtoff if t.tm_gmtoff is not None else -(
        time.altzone if t.tm_isdst > 0 else time.timezone)
    h, m = divmod(abs(off) // 60, 60)
    utc = 'UTC%s%d%s' % ('+' if off >= 0 else '-', h, ':%02d' % m if m else '')
    name = time.strftime('%Z', t)
    return ('%s, %s' % (name, utc)) if name and len(name) <= 5 and \
        name.isalpha() else utc


def prize(purse, place, tied=1):
    """What one player on `place` earns, sharing with `tied` others as
    twdb.tourney_standings does."""
    tied = max(1, tied or 1)
    return sum(twtourney.payout(purse, p)
               for p in range(place, place + tied)) // tied


def _place(r):
    """'1', or 'T1' when the place is shared."""
    return ('T%d' if r.get('tied', 1) > 1 else '%d') % r['place']


def _kind(r):
    """What sort of round this was, for a table cell.  A tournament round
    links to its event's page."""
    if r['kind'] == 'tourney':
        name = html.escape(r['event'] or 'Tournament')
        return ('<a class="pl" href="%s">%s</a>' % (event_href(r['day']), name)
                if r.get('day') is not None else name)
    return html.escape(twrecords.KIND_NAMES.get(r['kind'], r['kind']))


def _result(r):
    if r['result']:
        verdict = {'W': 'Won', 'L': 'Lost', 'T': 'Tied'}[r['result']]
        return '%s v %s' % (verdict, plink(r['opponent'])) if r['opponent'] \
            else verdict
    if r['place']:
        return '%s of %d' % (_ordinal(r['place']), r['field'])
    if not r.get('counted', True):
        return ('<span class="foot" title="A better round that day is the '
                'one on the tournament board">Not counted</span>')
    return ''


def _score(r):
    """Strokes, and to par where the round was a full eighteen."""
    if r['strokes'] is None:
        return '&ndash;'
    if r['to_par'] is None:
        return '%d <span class="foot">(%d holes)</span>' % (r['strokes'],
                                                           r['holes'])
    return '%d %s' % (r['strokes'], _topar(twrecords.fmt_par(r['to_par'])))


def _rounds_table(rs, who=True, course=True):
    """Date, (player,) type, (course,) score, result."""
    if not rs:
        return '<p class="foot" style="margin:0">No rounds yet.</p>'
    rnd = ' class="hide-sm"' if who else ''
    head = (('<th>Date</th>%s<th' + rnd + '>Round</th>%s'
             '<th class="num">Score</th><th>Result</th>')
            % ('<th>Golfer</th>' if who else '',
               '<th class="hide-sm">Course</th>' if course else ''))
    rows = ''.join(
        ('<tr><td>%s</td>%s<td' + rnd + '>%s</td>%s<td class="num">%s</td>'
         '<td>%s</td></tr>')
        % (_date(r['when']), '<td>%s</td>' % plink(r['persona']) if who else '',
           _kind(r),
           '<td class="hide-sm">%s</td>' % clink(r['course'], r['kind']) if course else '',
           _score(r), _result(r))
        for r in rs)
    return ('<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
            % (head, rows))


def _ordinal(n):
    """1 -> '1st'.  Same wording the game uses when it confirms a round."""
    if not n or n < 1:
        return '&ndash;'
    if 10 <= n % 100 <= 20:
        return '%dth' % n
    return '%d%s' % (n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


_LINK = re.compile(r'((?:href|action)=")(/[^"]*)"')


def _prefix_links(text):
    """Push every absolute link onto the mount point.

    Done once, on the finished page, rather than at each link: the pages are
    %-formatted templates, so threading a prefix through each href would mean
    renumbering every substitution tuple -- a lot of edits and an easy place to
    put a link in the wrong argument slot.  One rewrite at the end cannot miss
    one, and there is nothing else in these pages that looks like a link.
    """
    if not BASE:
        return text
    return _LINK.sub(lambda m: '%s%s%s"' % (m.group(1), BASE, m.group(2)), text)


NAV = (('/', 'Home'), ('/live', 'Live'), ('/leaderboard', 'Leaderboard'),
       ('/tournaments', 'Tournaments'), ('/stats', 'Stats'),
       ('/records', 'Records'), ('/courses', 'Courses'),
       ('/halloffame', 'Hall of Fame'))

# Shown only once there is an account to go to.  Offering it to a visitor who
# is not signed in would be a link that bounces them straight back here.
NAV_ACCOUNT = ('/account', 'Account')

# How often the live page reloads itself.  Short enough to feel live, long
# enough that a browser left open on it is not a load worth thinking about.
LIVE_REFRESH = 20


def _ago(seconds):
    """A duration as a person says it: "just now", "4m", "2h 10m"."""
    seconds = int(max(0, seconds))
    if seconds < 45:
        return 'just now'
    if seconds < 3600:
        return '%dm' % (seconds // 60)
    if seconds < 24 * 3600:
        hours, rest = divmod(seconds, 3600)
        return '%dh %dm' % (hours, rest // 60) if rest >= 60 else '%dh' % hours
    days, rest = divmod(seconds, 24 * 3600)
    return '%dd %dh' % (days, rest // 3600) if rest >= 3600 else '%dd' % days


def _span(seconds):
    """A length of time, for something that has been going on rather than
    something that just happened -- "up just now" reads like nonsense."""
    return 'less than a minute' if seconds < 60 else _ago(seconds)


def live_snapshot():
    """What the master server is doing this second, or None if it is not up.

    `lobbyd` mirrors its own state into the database -- see the LIVE PICTURE
    section there -- because the two are separate processes and this one cannot
    see the other's memory.  A row is only as true as its heartbeat, so a
    server that was killed reads as down here rather than as permanently busy.
    """
    try:
        up, age = DB.server_is_up()
        return {
            'up': up,
            'age': age,
            'online': DB.online() if up else [],
            'playing': DB.playing() if up else [],
            'started': (lambda v: float(v) if v else None)(DB.get_live('started')[0]),
        }
    except Exception:                                  # noqa: BLE001
        return None


def live_strip():
    """One line under the nav, on every page: is the server up, who is on it.

    The single question a visitor arrives with is "can I play right now?", and
    it should not take a click to answer.
    """
    snap = live_snapshot()
    if snap is None:
        return ''
    if not snap['up']:
        return ('<div class="strip"><div class="wrap">'
                '<span><span class="dot down"></span>Master server offline'
                '</span><a href="/live">Live &rarr;</a></div></div>')
    n, games = len(snap['online']), len(snap['playing'])
    who = ', '.join(html.escape(p['persona']) for p in snap['online'][:4])
    if n > 4:
        who += ' and %d more' % (n - 4)
    bits = ['<span><span class="dot up"></span>Master server online</span>',
            '<span>%s</span>' % ('Nobody in the lobby' if not n else
                                 '%d online &middot; %s' % (n, who))]
    if games:
        bits.append('<span>%d match%s in play</span>'
                    % (games, '' if games == 1 else 'es'))
    bits.append('<a href="/live">Live &rarr;</a>')
    return '<div class="strip"><div class="wrap">%s</div></div>' % ''.join(bits)


def lobby_endpoint(host_header=''):
    """(ipv4, port) for the master server, as a visitor must type it.

    The port is whatever `lobbyd` published when it started, so the download
    cannot drift from the server it is for.  The address is `--advertise` when
    given, and otherwise the hostname this request came in on -- which is the
    right answer when the site and the lobby are the same box, and is the only
    thing the site can know when they are not.

    Resolved at most once every ENDPOINT_TTL seconds.  A DNS lookup per page
    load would be absurd, and the answer changes about as often as the server
    moves.
    """
    when, cached = _ENDPOINT
    if cached and time.time() - when < ENDPOINT_TTL:
        return cached

    port = LOBBY_PORT
    try:
        published, _at = DB.get_live('port')
        if published:
            port = int(published)
    except (TypeError, ValueError, AttributeError):
        pass

    name = ADVERTISE or (host_header or '').split(',')[0].strip()
    name = name.rsplit(':', 1)[0] if name.count(':') == 1 else name
    name = name.strip('[]') or socket.gethostname()
    try:
        ip = socket.gethostbyname(name)
    except OSError:
        ip = ''
    # A loopback answer is kept rather than rejected: it is what a local test
    # should get, and an operator running this for real has --advertise.
    _ENDPOINT[:] = [time.time(), (ip, port)]
    return ip, port


# Where the server's source lives -- in every page's footer, so anyone can run
# their own.  TW05's server grew out of this project.
PROJECT_URL = 'https://github.com/jeddyhhh/TW04OnlineServer'


def pnach_name():
    return PNACH_NAME


def start_cash():
    """The starting balance lobbyd is using -- see twrecords.cash."""
    try:
        v, _at = DB.get_live('start_cash')
        return int(v) if v not in (None, '') else START_CASH
    except (TypeError, ValueError, AttributeError):
        return START_CASH


def cash(persona):
    return twrecords.cash(DB, persona, start_cash())


def _figure(value, label):
    return '<div class="figure"><b>%s</b><span>%s</span></div>' % (value, label)


def stats_board():
    """The server at a glance, on the foot of every page.

    Deliberately a handful of numbers rather than a report: the point is that a
    visitor can see the place is alive and being played on, which two dozen
    figures would obscure rather than show.
    """
    try:
        st = DB.stats()
    except Exception:                                  # noqa: BLE001
        return ''                                      # never break a page

    figures = [
        _figure(st['accounts'], 'Accounts'),
        _figure(st['personas'], 'Golfers'),
        _figure(st['recent'], 'Active this week'),
        _figure(st['matches'], 'Matches'),
        _figure(st['rounds'], 'Tournament rounds'),
        _figure(st['events'], 'Events scheduled'),
    ]
    if st['holes']:
        figures.append(_figure('{:,}'.format(st['holes']), 'Holes played'))
    # The live half, in the same row: what the place is doing now, beside what
    # it has ever done.
    figures.insert(0, _figure(
        '<span class="dot %s"></span>%s' % ('up' if st['up'] else 'down',
                                            st['online'] if st['up'] else '0'),
        'Online now'))
    if st['peak_online']:
        figures.append(_figure(st['peak_online'], 'Most at once'))

    notes = []
    if st['lobby_uptime']:
        notes.append('Lobby up <strong>%s</strong>' % _span(st['lobby_uptime']))
    elif not st['up']:
        notes.append('Lobby <strong>offline</strong>')
    if st['best_round']:
        notes.append('Best round <strong>%d</strong>' % st['best_round'])
    if st['longest_drive']:
        notes.append('Longest drive <strong>%d yd</strong>' % st['longest_drive'])
    if st['longest_putt']:
        notes.append('Longest putt <strong>%d ft</strong>' % st['longest_putt'])
    if st['eagles']:
        notes.append('<strong>%d</strong> eagle%s' % (st['eagles'],
                                                      '' if st['eagles'] == 1 else 's'))
    if st['aces']:
        notes.append('<strong>%d</strong> hole%s in one'
                     % (st['aces'], '' if st['aces'] == 1 else 's'))
    if st['top_course']:
        course, n = st['top_course']
        notes.append('Most played <strong>%s</strong> (%d)'
                     % (html.escape(twstats.course_name(course)), n))
    if st['since']:
        notes.append('Online since %s'
                     % time.strftime('%d %b %Y', time.localtime(st['since'])))

    return ('<h2>Server stats</h2><div class="figures">%s</div>%s'
            % (''.join(figures),
               '<p class="foot" style="margin-top:1.1rem">%s</p>'
               % ' &middot; '.join(notes) if notes else ''))


# Headers for the reports page: never cached, never indexed, and never leak
# its address to another site in a Referer.
PRIVATE = (('Cache-Control', 'no-store'),
           ('X-Robots-Tag', 'noindex, nofollow'),
           ('Referrer-Policy', 'no-referrer'))


def reports_key(db_path, given='', name='reports.key'):
    """The secret in the reports (or admin) page's address.

    `--reports-key` wins; otherwise it is read from `reports.key` beside the
    database, and made there the first time.  Beside the database rather than
    beside this file, so a deployment copied over the top of an old one keeps
    the same address.  The admin page's is `admin.key`, made the same way.
    """
    if given:
        return given
    path = os.path.join(os.path.dirname(os.path.abspath(db_path)), name)
    try:
        with open(path, encoding='ascii') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(key + '\n')
    return key


def page(title, body, message=None, kind='err', stats=True, refresh=0,
         signed_in=False):
    body = _prefix_links(body)
    banner = ('<div class="msg %s">%s</div>' % (kind, html.escape(message))
              if message else '')
    links = (NAV + (NAV_ACCOUNT,)) if signed_in else NAV
    nav = ''.join('<a href="%s">%s</a>' % (u(href), text) for href, text in links)
    # The same links twice: a row for a wide screen, and for a phone a MENU
    # button that drops them down as a list.  CSS shows one or the other; the
    # drop-down is a <details>, so it opens and shuts with no script, and
    # following a link loads a page with it shut again.
    nav = ('<nav>%s</nav><details class="menu"><summary>Menu</summary>'
           '<div class="menu-list">%s</div></details>' % (nav, nav))
    board = _prefix_links(stats_board()) if stats else ''
    strip = _prefix_links(live_strip())
    meta = ('<meta http-equiv="refresh" content="%d">' % refresh) if refresh else ''
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">%s
<title>TW05Online - %s</title><style>%s</style></head><body>
<header class="top"><div class="wrap">
<p class="brand">Tiger Woods <span>PGA Tour 2005</span><small>Online &mdash; community master server</small></p>
%s</div></header>%s
<main class="wrap">%s%s</main>
<footer><div class="wrap">%s
<p class="foot" style="margin-top:1.4rem">This server grew out of TW04 Online
Server, which is free and open source &mdash; <strong>you can run your
own</strong>: <a href="%s">%s</a></p>
<p class="foot" style="margin-top:.4rem">Tiger Woods PGA Tour 2005 is a
trademark of its owners. This is a fan-run server and is not affiliated with
them.</p></div></footer>
</body></html>""" % (meta, html.escape(title), CSS, nav, strip, banner, body,
                     board, PROJECT_URL, PROJECT_URL.split('://', 1)[1])
            ).encode('utf-8')


def new_session(account_id):
    token = secrets.token_urlsafe(24)
    with SESSION_LOCK:
        now = time.time()
        for old, data in list(SESSIONS.items()):
            if now - data['seen'] > SESSION_MAX_AGE:
                del SESSIONS[old]
        SESSIONS[token] = {'account': account_id, 'csrf': secrets.token_urlsafe(16),
                           'seen': now}
    return token


def get_session(token):
    with SESSION_LOCK:
        data = SESSIONS.get(token)
        if not data:
            return None
        if time.time() - data['seen'] > SESSION_MAX_AGE:
            del SESSIONS[token]
            return None
        data['seen'] = time.time()
        return dict(data)


def throttled(ip):
    """True when this address has failed too often lately."""
    count, first = ATTEMPTS.get(ip, (0, 0.0))
    if time.time() - first > ATTEMPT_WINDOW:
        ATTEMPTS.pop(ip, None)
        return False
    return count >= MAX_ATTEMPTS


def note_failure(ip):
    count, first = ATTEMPTS.get(ip, (0, time.time()))
    ATTEMPTS[ip] = (count + 1, first)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'tw05-webui'

    def version_string(self):
        # Just the name.  The default adds "Python/3.x.y", which only helps
        # someone looking for a known hole in that version.
        return self.server_version
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        LOG.write('%s %s %s' % (time.strftime('%H:%M:%S'), self.peer(),
                                fmt % args))

    # -- plumbing ----------------------------------------------------------
    def reply(self, body, status=200, cookie=None, location=None,
              ctype='text/html; charset=utf-8', disposition=None, headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if location:
            self.send_header('Location', location)
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        # Never inside another site's frame -- that is how a page gets a
        # visitor to click a sign-in or account button they cannot see.
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        if SECURE_COOKIE:
            # A site that marks its cookie Secure is served over HTTPS; tell
            # browsers to stay on it.  Browsers ignore this over plain HTTP,
            # so the LAN build (--no-secure-cookie) simply does not send it.
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        self.send_header('Referrer-Policy', 'same-origin')
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, where, cookie=None):
        self.reply(b'', status=303, cookie=cookie, location=where)

    def form(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 64 * 1024:
            return {}
        raw = self.rfile.read(length).decode('utf-8', 'replace')
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def session(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return None, None
        jar = http.cookies.SimpleCookie(raw)
        token = jar[COOKIE].value if COOKIE in jar else None
        return token, (get_session(token) if token else None)

    def route(self):
        """The path with the mount prefix removed, or None if it is outside it.

        `/TW05Online` and `/TW05Online/` both mean the front page.
        """
        path = urllib.parse.urlparse(self.path).path
        if not BASE:
            return path
        if path == BASE:
            return '/'
        if path.startswith(BASE + '/'):
            return path[len(BASE):]
        return None

    def peer(self):
        """The client's address, which behind a proxy is not the socket's.

        Without this every request comes from 127.0.0.1 and the login throttle
        becomes one shared bucket: a handful of wrong passwords from anyone
        locks out everyone.  X-Forwarded-For is trivially forged, though, so it
        is only believed when the operator says there is a proxy in front.
        """
        if TRUST_PROXY:
            fwd = self.headers.get('X-Forwarded-For')
            if fwd:
                return fwd.split(',')[0].strip()
        return self.client_address[0]

    def cookie_for(self, token):
        # Scoped to the mount point: a cookie for /TW05Online must not be sent
        # to the rest of the site sharing this hostname.  `Secure` keeps it off
        # plain HTTP entirely -- without it, one http:// link is enough to put
        # a live session token on the wire in clear.
        return ('%s=%s; Path=%s; HttpOnly; SameSite=Lax%s; Max-Age=%d'
                % (COOKIE, token, BASE or '/', '; Secure' if SECURE_COOKIE else '',
                   SESSION_MAX_AGE))

    # -- pages -------------------------------------------------------------
    def do_GET(self):
        path = self.route()
        if path is None:
            return self.reply(page('Not found', '<h1>Not found</h1>'), status=404)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        note = (query.get('m') or [None])[0]
        _, session = self.session()

        if path == '/':
            return self.reply(self.front(note, session=session))
        if path == '/account':
            if not session:
                return self.redirect(u('/'))
            return self.reply(self.profile(session, note))
        if path == '/' + pnach_name():
            body = self.pnach()
            if body is None:
                return self.reply(page(
                    'Patch', '<div class="card"><h2>Patch</h2>'
                    '<p class="sub" style="margin:0">This server does not know '
                    'its own public address, so it cannot build a patch that '
                    'points at itself. The operator needs to start it with '
                    '<code>--advertise</code>.</p></div>'), status=503)
            return self.reply(body, ctype='text/plain; charset=utf-8',
                              disposition='attachment; filename="%s"'
                                          % pnach_name())
        if path in ('/' + CHT_NAME, '/' + CHEATDEVICE_NAME):
            ip, _port = lobby_endpoint(self.headers.get('Host', ''))
            if not ip:
                return self.reply(page(
                    'Cheat codes', '<div class="card"><h2>Cheat codes</h2>'
                    '<p class="sub" style="margin:0">This server does not know '
                    'its own public address, so it cannot build codes that '
                    'point at itself. The operator needs to start it with '
                    '<code>--advertise</code>.</p></div>'), status=503)
            build = build_cht if path[1:] == CHT_NAME else build_cheatdevice
            return self.reply(build(ip).encode('utf-8'),
                              ctype='text/plain; charset=utf-8',
                              disposition='attachment; filename="%s"'
                                          % path[1:])
        if path == '/live':
            return self.reply(self.live(session))
        if path == '/live.json':
            return self.reply(self.live_json(), ctype='application/json')
        if path == '/leaderboard':
            return self.reply(self.leaderboard(session))
        if path == '/stats':
            return self.reply(self.stats_page(session))
        if path == '/records':
            return self.reply(self.records_page(session))
        if path == '/courses':
            return self.reply(self.courses_page(session))
        if path.startswith('/course/'):
            return self.course_page(path[len('/course/'):], session)
        if path.startswith('/player/'):
            return self.player_page(
                urllib.parse.unquote(path[len('/player/'):]), session)
        if path == '/tournaments':
            return self.reply(self.tournaments(session))
        if path == '/compare':
            return self.reply(self.compare_page(
                (query.get('a') or [''])[0].strip(),
                (query.get('b') or [''])[0].strip(), session))
        if path == '/halloffame':
            return self.reply(self.hall_of_fame(session))
        if path.startswith('/event/'):
            return self.event_page(path[len('/event/'):], session)
        if path.startswith('/h2h/'):
            names = path[len('/h2h/'):].split('/')
            if len(names) != 2:
                return self.not_found('Head to head', 'That is not a pair of '
                                      'golfers.', session)
            return self.h2h_page(*[urllib.parse.unquote(n) for n in names],
                                 session=session)
        if path.startswith('/admin/'):
            if not self.admin_allowed(path[len('/admin/'):]):
                return self.reply(page('Not found', '<h1>Not found</h1>'),
                                  status=404)
            return self.reply(self.admin_page(
                (query.get('q') or [''])[0], note), headers=PRIVATE)
        if path.startswith('/reports/'):
            if not self.reports_allowed(path[len('/reports/'):]):
                return self.reply(page('Not found', '<h1>Not found</h1>'),
                                  status=404)
            return self.reply(self.reports_page(note), headers=PRIVATE)
        if path == '/logout':
            token, _ = self.session()
            with SESSION_LOCK:
                SESSIONS.pop(token, None)
            return self.redirect(u('/'), cookie='%s=; Path=%s; Max-Age=0' % (COOKIE, BASE or '/'))
        self.reply(page('Not found', '<h1>Not found</h1>'), status=404)

    def do_POST(self):
        path = self.route()
        if path is None:
            return self.reply(page('Not found', '<h1>Not found</h1>'), status=404)
        fields = self.form()
        _token, session = self.session()

        if path.startswith('/reports/') and path.endswith('/handle'):
            # The key in the path is the credential AND the CSRF token: a
            # third-party page cannot build this form without knowing it.
            key = path[len('/reports/'):-len('/handle')]
            if not self.reports_allowed(key):
                return self.reply(page('Not found', '<h1>Not found</h1>'),
                                  status=404)
            return self.do_report_handled(key, fields)

        if path.startswith('/admin/'):
            # As with the reports page, the key in the path is the credential
            # and the CSRF token at once.
            key, _, action = path[len('/admin/'):].partition('/')
            if not self.admin_allowed(key):
                return self.reply(page('Not found', '<h1>Not found</h1>'),
                                  status=404)
            return self.do_admin(action, fields)

        if path in ('/register', '/login'):
            if throttled(self.peer()):
                return self.reply(self.front('too many attempts -- wait a few '
                                             'minutes and try again'), status=429)
            return (self.do_register(fields) if path == '/register'
                    else self.do_login(fields))

        if not session:
            return self.redirect(u('/'))
        if not secrets.compare_digest(fields.get('csrf', ''), session['csrf']):
            return self.reply(self.profile(session, 'that form expired -- '
                                           'please try again'), status=400)

        actions = {
            '/persona/add': self.do_add_persona,
            '/persona/drop': self.do_drop_persona,
            '/profile': self.do_profile,
            '/password': self.do_password,
        }
        action = actions.get(path)
        if not action:
            return self.reply(page('Not found', '<h1>Not found</h1>'), status=404)
        try:
            note = action(session, fields)
        except twdb.Error as exc:
            return self.reply(self.profile(session, str(exc)))
        return self.reply(self.profile(session, note, kind='ok'))

    # -- actions -----------------------------------------------------------
    def do_register(self, fields):
        try:
            if fields.get('password') != fields.get('password2'):
                raise twdb.Error('the two passwords do not match')
            account_id = DB.create_account(
                fields.get('account', ''), fields.get('password', ''),
                mail=fields.get('mail', '')[:120],
                gend=fields.get('gend', 'M')[:1] or 'M',
                born=(fields.get('born', '') or '19700101').replace('-', '')[:8],
                persona=fields.get('persona') or fields.get('account', ''))
        except twdb.Error as exc:
            note_failure(self.peer())
            return self.reply(self.front(str(exc), fields))
        return self.redirect(u('/account'), cookie=self.cookie_for(new_session(account_id)))

    def do_login(self, fields):
        try:
            row = DB.verify(fields.get('account', ''), fields.get('password', ''))
        except twdb.Error as exc:
            return self.reply(self.front(str(exc), fields))
        if not row:
            note_failure(self.peer())
            return self.reply(self.front('no account by that name, or the wrong '
                                         'password', fields))
        return self.redirect(u('/account'), cookie=self.cookie_for(new_session(row['id'])))

    def do_add_persona(self, session, fields):
        name = fields.get('persona', '')
        DB.add_persona(session['account'], name)
        return 'added the persona %s' % name

    def do_drop_persona(self, session, fields):
        name = fields.get('persona', '')
        DB.drop_persona(session['account'], name)
        return 'removed the persona %s' % name

    def do_profile(self, session, fields):
        DB.set_profile(session['account'], mail=fields.get('mail', '')[:120],
                       gend=(fields.get('gend') or 'M')[:1],
                       born=(fields.get('born', '') or '19700101').replace('-', '')[:8],
                       spam='spam' in fields)
        return 'profile saved'

    def do_password(self, session, fields):
        row = DB.one('SELECT name FROM accounts WHERE id = ?', (session['account'],))
        if not DB.verify(row['name'], fields.get('current', '')):
            raise twdb.Error('your current password is not right')
        if fields.get('password') != fields.get('password2'):
            raise twdb.Error('the two new passwords do not match')
        DB.set_password(session['account'], fields.get('password', ''))
        return 'password changed'

    # -- rendering ---------------------------------------------------------
    def front(self, note=None, fields=None, session=None):
        """The front page, which is the same page whether or not you are on it.

        Someone who has just signed up wants the connection instructions next,
        and they are here -- so a signed-in visitor gets the same page with the
        two forms replaced by a line saying who they are.
        """
        got = fields or {}
        keep = lambda k: html.escape(got.get(k, ''), quote=True)   # noqa: E731
        if session:
            who = ''
            try:
                row = DB.one('SELECT name FROM accounts WHERE id = ?',
                             (session['account'],))
                who = row['name'] if row else ''
            except Exception:                          # noqa: BLE001
                who = ''
            body = ('<div class="card"><h2>Signed in</h2>'
                    '<p class="sub" style="margin:0">You are signed in%s. '
                    '<a href="/account">Manage your account</a> to add a '
                    'persona or change your password &mdash; or read on for '
                    'how to connect.</p></div>'
                    % (' as <strong>%s</strong>' % html.escape(who)
                       if who else ''))
            return page('Home',
                        self.today_card() + body + self.connect_card()
                        + self.disc_card() + self.real_ps2_card(), note, signed_in=True)
        body = """
<div class="card">
<h2>Sign in</h2>
<form method="post" action="/login">
  <div class="row">
    <div><label>Account name</label>
      <input type="text" name="account" required autocomplete="username"></div>
    <div><label>Password</label>
      <input type="password" name="password" required
             autocomplete="current-password"></div>
  </div>
  <button type="submit" class="quiet">Sign in</button>
</form>
</div>

<div class="card">
<h2>Create an account</h2>
<p class="sub" style="margin:-.4rem 0 1rem">This is the account you sign in with
on the console.</p>
<form method="post" action="/register">
  <div class="row">
    <div><label>Account name</label>
      <input type="text" name="account" maxlength="%d" value="%s" required
             autocomplete="username"></div>
    <div><label>First persona <span class="foot">(the name other
      players see)</span></label>
      <input type="text" name="persona" maxlength="%d" value="%s"></div>
  </div>
  <div class="row">
    <div><label>Password (%d&ndash;%d characters)</label>
      <input type="password" name="password" required
             autocomplete="new-password"></div>
    <div><label>Repeat password</label>
      <input type="password" name="password2" required
             autocomplete="new-password"></div>
  </div>
  <div class="row">
    <div><label>Email (optional)</label>
      <input type="email" name="mail" value="%s"></div>
    <div><label>Gender</label><select name="gend">
      <option value="M">Male</option><option value="F">Female</option>
    </select></div>
  </div>
  <button type="submit">Create account</button>
</form>
</div>
""" % (twdb.MAX_NAME, keep('account'), twdb.MAX_NAME, keep('persona'),
       twdb.MIN_PASSWORD, twdb.MAX_PASSWORD, keep('mail'))
        return page('Home',
                    self.today_card() + body + self.connect_card()
                    + self.disc_card() + self.real_ps2_card(), note)

    def today_card(self):
        """Today's event, above the fold.  It is the one thing on this site
        that changes every day, so it earns the top of the page."""
        day = twtourney.today()
        event = DB.event(day)
        if not event:
            return ''
        board = DB.tourney_day(day, limit=3)
        played = ''
        if board:
            rows = ''.join(
                '<tr><td class="rank%s">%d</td><td>%s</td>'
                '<td class="num">%d</td><td class="num %s">%s</td></tr>'
                % (' rank-1' if r['place'] == 1 else '', r['place'],
                   plink(r['name']), r['strokes'],
                   'under' if twtourney.to_par(
                       r['fields'], r['par']).startswith('-') else 'over',
                   twtourney.to_par(r['fields'], r['par']))
                for r in board)
            played = ('<table style="margin-top:1rem"><caption>Leading today'
                      '</caption><tbody>%s</tbody></table>' % rows)
        else:
            played = ('<p class="foot" style="margin-top:1rem">Nobody has '
                      'posted a score yet today.</p>')
        if board:
            played += ('<p class="foot" style="margin:.6rem 0 0"><a class="pl" '
                       'href="%s">Full leaderboard</a></p>' % event_href(day))
        return ('<div class="card"><h2>Today&rsquo;s event &middot; '
                '<span style="color:var(--ink)">closes in %s</span> '
                '<span class="foot" style="letter-spacing:0;text-transform:'
                'none">at midnight %s</span></h2>'
                '<h1 style="margin:0 0 .2rem">%s</h1>'
                '<p class="sub" style="margin:0">%s &middot; %s &middot; '
                '%s</p><p style="margin:.5rem 0 0">%s</p>%s</div>'
                % (closes_in(), server_tz(), html.escape(event['name']),
                   html.escape(twstats.course_name(event['course'])),
                   twtourney.money(event['purse']),
                   twtourney.from_day(day).strftime('%A %d %B %Y'),
                   conditions_line(event.get('conditions')), played))

    def connect_card(self):
        """How to get the game talking to this server, with the patch to do it.

        The patch is built for THIS server: it gets the game past DNAS and
        writes this server's address over EA's host names (HOST_SLOTS), so
        there is nothing to set in PCSX2 beyond turning the network on.
        """
        ip, port = lobby_endpoint(self.headers.get('Host', ''))
        if ip:
            download = ('<p><a class="dl" href="/%s">Download %s</a></p>'
                        % (pnach_name(), pnach_name()))
        else:
            download = (
                '<p class="msg err" style="margin:0 0 1.2rem">This site cannot '
                'work out its own public address, so it cannot build a patch '
                'that points at itself. The server operator needs to start it '
                'with <code>--advertise</code>.</p>')
        where = ('<code>%s</code> port <code>%d</code>' % (html.escape(ip), port)
                 if ip else 'this server')
        return """
<div class="card">
<h2>Connect from PCSX2</h2>
<p class="sub" style="margin:-.2rem 0 1.2rem">One patch file points the game
at %s and gets it past the dead DNAS servers. Nothing is written to your ISO
&mdash; deleting the file puts everything back.</p>

%s

<h3>1. Install the patch</h3>
<ol class="steps">
<li>Put the downloaded file in your PCSX2 <code>cheats</code> folder
  (<em>Settings &rarr; Folders</em>, or <code>&lt;PCSX2&gt;/cheats</code> next
  to the executable). Keep the filename exactly as it downloaded &mdash; PCSX2
  matches it against the disc&rsquo;s serial and CRC.</li>
<li>Right-click the game in your list &rarr; <em>Properties</em> &rarr;
  <em>Patches</em>, and tick <strong>Enable Cheats</strong>.</li>
<li>Start the game. The patch applies at boot, so a game already running is
  still pointed at EA.</li>
</ol>

<h3>2. Turn the network on</h3>
<ol class="steps">
<li><em>Settings &rarr; Network &amp; HDD</em>: tick
  <strong>Enable Network (DEV9)</strong>, and set the Ethernet device mode to
  <strong>Sockets</strong> unless you know you want PCAP.</li>
<li><strong>Ethernet Device</strong>: on Windows, the adapter you connect to
  the internet with. On Linux and the Steam Deck, leave it on
  <strong>Auto</strong>.</li>
<li>Leave the DNS settings and the host list alone &mdash; the patch has the
  address in it. (If you set up host overrides for this game before, you can
  delete them.)</li>
</ol>

<h3>3. Sign up, then sign in</h3>
<ol class="steps">
<li>Create an account on this page. The <strong>account name</strong> and
  <strong>password</strong> are what you type on the console; the
  <strong>persona</strong> is the name other players see. An account can hold
  up to four personas.</li>
<li><strong>First time only:</strong> the game needs a PlayStation&nbsp;2
  network configuration on the memory card. If there is none it offers to
  make one &mdash; just save the defaults.</li>
<li>Choose <em>PLAY ONLINE</em>, let it pass DNAS, then on <em>SELECT EA
  ACCOUNT</em> pick <em>USE EXISTING EA ACCOUNT</em> and type the account
  name and password. Next time it offers <em>USE SAVED ACCOUNT</em>. (TW05
  also offers an account TW04 saved to the card &mdash; that is fine if it was
  made on this server.)</li>
</ol>

<h3>What you can play</h3>
<ol class="steps">
<li><strong>Head to head</strong>, in four modes: stroke play, match play,
  the <strong>3 Hole Mini-Game</strong> (three random holes from random
  courses), and <strong>Battle</strong> &mdash; match play where winning a
  hole lets you take a club out of your opponent&rsquo;s bag, or put one back
  in yours. <em>CREATE GAME</em> is the SELECT button; anyone in the room can
  join it.</li>
<li><strong>The daily tournament</strong>: one event a day, the same course
  and conditions for everyone, scored against everyone else&rsquo;s round
  that day.</li>
<li><strong>Cash</strong>: every golfer starts with %s. Tournament prize
  money is added to it, and head-to-head games can be played for a wager
  &mdash; both stakes go in the pot and the winner takes it.</li>
</ol>

<h3>If it will not connect</h3>
<p class="foot">Check the <a href="/live">live page</a> first &mdash; if the
master server is offline, nothing will connect. If the game stops at DNAS,
the patch is not active: Enable Cheats is off, the file was renamed, or the
game was started before the file was in place. If it passes DNAS and then
cannot find the server, download the patch again &mdash; it carries the
server&rsquo;s address, so one from before a move points at the old one &mdash;
and if that is not it, try setting <strong>DNS1</strong> to
<strong>Internal</strong>. For head-to-head play the two consoles talk
<strong>directly</strong> to each other over UDP port 3658 once the server has
introduced them, so each must be reachable from the other &mdash; a Wi-Fi
network with client isolation lets you into the lobby and then fails to
start the match. Two copies of PCSX2 on the same Windows PC cannot play each
other; use two machines.</p>
</div>""" % (where, download, twtourney.money(start_cash()))

    def disc_card(self):
        """Exactly which release the patch is for, and how to check yours.

        PCSX2 matches a .pnach to a game by SERIAL and CRC -- they are the
        filename -- so a disc that reports anything else is silently not
        patched, and stops at DNAS.
        """
        return """
<div class="card">
<h2>The disc you need</h2>
<p class="sub" style="margin:-.2rem 0 1.2rem">The patch is a list of addresses
inside one specific build of the game, so it will not apply to any other
release. PCSX2 shows the serial and CRC of whatever you have in its game
list.</p>

<table><tbody>
<tr><td>Title</td><td><strong>Tiger Woods PGA Tour 2005</strong></td></tr>
<tr><td>Platform</td><td>PlayStation 2</td></tr>
<tr><td>Region</td><td>USA &mdash; NTSC-U/C</td></tr>
<tr><td>Serial</td><td><code>%s</code></td></tr>
<tr><td>CRC</td><td><code>%s</code></td></tr>
</tbody></table>

<p class="foot">Dump your own disc &mdash; a PS2 game you own reads on most PC
DVD drives, and PCSX2 documents the process. You will also need a PS2 BIOS
image, which has to come from a console you own; PCSX2 cannot run without one
and none is distributed here.</p>
</div>""" % (SERIAL, CRC)

    def pnach(self):
        """The patch, built for THIS server rather than shipped as a fixed
        file, so the address in it cannot be stale.  None when this site
        does not know its own address."""
        ip, _port = lobby_endpoint(self.headers.get('Host', ''))
        if not ip:
            return None
        return (PNACH % {'serial': SERIAL, 'ip': ip,
                         'hosts': host_patches(ip)}).encode('utf-8')

    def real_ps2_card(self):
        """Codes for a real console -- clearly marked as never tried."""
        return """
<div class="card">
<h2>Real PS2 &middot; untested</h2>
<p class="msg err" style="margin:0 0 1.2rem"><strong>100%% untested.</strong>
Nobody has tried these on a real console yet. They are the same patch as the
PCSX2 file above, turned into cheat codes, and they may not work at all. If you
try them &mdash; working or not &mdash; please report what happened on
<a href="%s/issues">the project&rsquo;s GitHub page</a>.</p>

<p><a class="dl" href="/%s">Download %s</a> &nbsp;
<a class="dl" href="/%s">Download %s</a></p>

<h3>What you need</h3>
<ol class="steps">
<li>A PS2 that can run homebrew (for example with FreeMcBoot), with a network
  adapter &mdash; built in on slim models, the official adapter on the older
  ones.</li>
<li>A network configuration saved on the memory card. The codes carry this
  server&rsquo;s address, so no special DNS is needed. For head-to-head
  matches, forward UDP port 3658 to the PS2.</li>
</ol>

<h3>Open PS2 Loader</h3>
<ol class="steps">
<li>Put <code>%s</code> in the <code>CHT</code> folder on the drive your games
  are on, keeping its name.</li>
<li>In the game&rsquo;s settings in OPL, turn cheats on and pick
  <em>Auto select cheats</em>.</li>
<li>Load the game from USB or an internal hard drive. Loading it over the
  network (SMB) probably clashes with the game using the network adapter
  itself.</li>
</ol>

<h3>Cheat Device</h3>
<ol class="steps">
<li>Copy the contents of <code>%s</code> into your Cheat Device cheat list,
  and turn on both <em>Master Code</em> and <em>TW05 Online</em> before
  booting the disc.</li>
</ol>
</div>""" % (PROJECT_URL, CHT_NAME, CHT_NAME, CHEATDEVICE_NAME,
             CHEATDEVICE_NAME, CHT_NAME, CHEATDEVICE_NAME)

    # -- the live page ------------------------------------------------------
    FEED_WORDS = {
        'login': 'arrived', 'logout': 'left', 'room': 'lobby',
        'match': 'tee off', 'result': 'result', 'round': 'round',
        'signup': 'new player', 'calendar': 'calendar', 'server': 'server',
    }

    def live(self, session):
        """Who is on the server right now, and what has just happened.

        The page reloads itself rather than polling with script: it is a few
        kilobytes, there is nothing on it to lose across a reload, and a meta
        refresh works with script switched off.
        """
        snap = live_snapshot()
        if snap is None:
            return page('Live', '<div class="card"><h2>Live</h2>'
                                '<p class="sub" style="margin:0">The server '
                                'status is not available.</p></div>',
                        signed_in=bool(session))

        if not snap['up']:
            last = ('last heard from %s ago' % _ago(snap['age'])
                    if snap['age'] else 'it has not checked in')
            card = ('<div class="card"><h2>Master server</h2>'
                    '<h1 style="margin:0"><span class="dot down"></span>'
                    'Offline</h1>'
                    '<p class="sub" style="margin:.4rem 0 0">The lobby is not '
                    'answering &mdash; %s. The web site keeps working; the '
                    'game will not connect until it is back.</p></div>' % last)
            return page('Live', card + self.feed_card(),
                        refresh=LIVE_REFRESH, signed_in=bool(session))

        cards = []
        uptime = ('up %s' % _span(time.time() - snap['started'])
                  if snap['started'] else 'up')
        games = len(snap['playing'])
        cards.append(
            '<div class="card"><h2>Master server</h2>'
            '<h1 style="margin:0"><span class="dot up"></span>Online</h1>'
            '<p class="sub" style="margin:.4rem 0 0">%s &middot; last check-in '
            '%s &middot; %d in the lobby &middot; %d match%s in play</p></div>'
            % (uptime, _ago(snap['age']), len(snap['online']), games,
               '' if games == 1 else 'es'))

        if snap['online']:
            rows = []
            now = time.time()
            for player in snap['online']:
                tag = ('<span class="tag playing">playing</span>'
                       if player['state'] == 'playing' else '')
                rows.append('<tr><td><strong>%s</strong> %s</td><td>%s</td>'
                            '<td class="num">%s</td></tr>'
                            % (plink(player['persona']), tag,
                               html.escape(player['detail']
                                           or 'choosing a lobby'),
                               _ago(now - player['since'])))
            cards.append('<div class="card"><h2>In the lobby</h2>'
                         '<table><thead><tr><th>Player</th><th>Where</th>'
                         '<th class="num">Here for</th></tr></thead><tbody>'
                         + ''.join(rows) + '</tbody></table></div>')
        else:
            cards.append('<div class="card"><h2>In the lobby</h2>'
                         '<p class="sub" style="margin:0">Nobody is online at '
                         'the moment &mdash; the server is up and waiting.'
                         '</p></div>')

        if snap['playing']:
            rows = []
            now = time.time()
            for game in snap['playing']:
                course = (twstats.course_name(game['course'])
                          if game['course'] >= 0 else '')
                rows.append('<tr><td><strong>%s</strong> v <strong>%s</strong>'
                            '</td><td>%s</td><td>%s</td><td class="num">%s</td>'
                            '</tr>'
                            % (plink(game['host']),
                               plink(game['guest']),
                               html.escape(game['kind'] or ''),
                               html.escape(course) or '&mdash;',
                               _ago(now - game['started'])))
            cards.append('<div class="card"><h2>Matches in play</h2>'
                         '<table><thead><tr><th>Players</th><th>Type</th>'
                         '<th>Course</th><th class="num">Running</th></tr>'
                         '</thead><tbody>' + ''.join(rows)
                         + '</tbody></table></div>')

        return page('Live', ''.join(cards) + self.feed_card(),
                    refresh=LIVE_REFRESH, signed_in=bool(session))

    def feed_card(self, limit=25):
        """The last few things that happened, newest first."""
        try:
            rows = DB.activity(limit=limit)
        except Exception:                              # noqa: BLE001
            return ''
        if not rows:
            return ('<div class="card"><h2>Activity</h2><p class="sub" '
                    'style="margin:0">Nothing has happened yet.</p></div>')
        now = time.time()
        items = []
        for row in rows:
            when = _ago(now - row['at'])
            items.append('<li><time>%s</time><span class="k">%s</span>'
                         '<span>%s</span></li>'
                         % (when if when == 'just now' else when + ' ago',
                            html.escape(self.FEED_WORDS.get(row['kind'],
                                                            row['kind'])),
                            html.escape(row['text'])))
        return ('<div class="card"><h2>Activity</h2><ul class="feed">%s</ul>'
                '</div>' % ''.join(items))

    def live_json(self):
        """The same picture as data, for anyone who would rather poll it.

        Nothing in here that is not already drawn on the page -- names, rooms
        and counts.  No addresses and no account names.
        """
        snap = live_snapshot() or {'up': False, 'online': [], 'playing': [],
                                   'age': 0, 'started': None}
        out = {
            'up': bool(snap['up']),
            'checked_in': round(snap['age'], 1),
            'started': snap['started'],
            'online': [{'persona': p['persona'], 'state': p['state'],
                        'where': p['detail'], 'since': p['since']}
                       for p in snap['online']],
            'playing': [{'host': g['host'], 'guest': g['guest'],
                         'kind': g['kind'], 'course': g['course'],
                         'course_name': (twstats.course_name(g['course'])
                                         if g['course'] >= 0 else ''),
                         'started': g['started']}
                        for g in snap['playing']],
        }
        try:
            out['activity'] = [{'at': r['at'], 'kind': r['kind'],
                                'text': r['text']} for r in DB.activity(25)]
        except Exception:                              # noqa: BLE001
            out['activity'] = []
        return json.dumps(out, indent=1).encode('utf-8')

    def leaderboard(self, session):
        """Everyone who has finished a match, best first.

        Strokes per hole rather than total strokes: a Front 9 is nine holes and
        a full round is eighteen, so the totals are not comparable.
        """
        rows = []
        for n, e in enumerate(DB.leaderboard(), 1):
            rows.append(
                '<tr><td class="rank%s">%d</td><td><strong>%s</strong></td>'
                '<td class="num">%d&ndash;%d&ndash;%d</td><td class="num">%.2f</td>'
                '<td class="num">%d</td><td class="num">%d</td>'
                '<td class="num">%d</td><td class="num">%d</td></tr>'
                % (' rank-1' if n == 1 else '', n, plink(e['name']),
                   e['won'], e['lost'], e['tied'], e['per_hole'],
                   e['birdies'], e['eagles'], e['aces'], e['longest']))
        table = ('<table><thead><tr><th>#</th><th>Golfer</th>'
                 '<th class="num">W&ndash;L&ndash;T</th>'
                 '<th class="num">Str/hole</th><th class="num">Birdies</th>'
                 '<th class="num">Eagles</th><th class="num">Aces</th>'
                 '<th class="num">Longest</th></tr></thead><tbody>'
                 + ''.join(rows) + '</tbody></table>') if rows else (
            '<p class="foot" style="margin:0">Nobody has finished a match yet.</p>')
        body = ('<h1>Leaderboard</h1><p class="sub">Head to head, ranked by '
                'games won and then by strokes per hole &mdash; a Front 9 and '
                'a full round are not comparable any other way.</p>'
                '<div class="card scroll"><h2>Head to head &middot; every mode'
                '</h2>%s</div>%s' % (table, self.cash_board()))
        return page('Leaderboard', body, signed_in=bool(session))

    def cash_board(self, top=20):
        """The richest golfers: everyone who has played or wagered, by their
        cash.  Starting cash is the same for all, so it is what they have
        done with it that orders the table."""
        names = {r['persona'] for r in twrecords.rounds(DB)}
        names |= {r['persona'] for r in DB.query('SELECT DISTINCT persona '
                                                 'FROM cash')}
        board = sorted(((n, cash(n)) for n in names),
                       key=lambda t: (-t[1]['balance'], t[0].lower()))[:top]
        if not board:
            return ''
        rows = ''.join(
            '<tr><td class="rank%s">%d</td><td><strong>%s</strong></td>'
            '<td class="num hide-sm">%s</td><td class="num">%s</td>'
            '<td class="num hide-sm">%s</td><td class="num"><strong>%s</strong>'
            '</td></tr>'
            % (' rank-1' if n == 1 else '', n, plink(who),
               twtourney.money(c['tourney']) if c['tourney'] else '&ndash;',
               _signed(c['earned'] - c['lost']) if c['wagers'] else '&ndash;',
               '%d of %d' % (c['won'], c['wagers']) if c['wagers'] else '&ndash;',
               twtourney.money(c['balance']))
            for n, (who, c) in enumerate(board, 1))
        return ('<div class="card scroll"><h2>Cash</h2><p class="foot" '
                'style="margin:-.4rem 0 .8rem">Everyone starts with %s. '
                'Tournament prize money adds to it, and wagers move it between '
                'players.</p><table><thead><tr><th>#</th><th>Golfer</th>'
                '<th class="num hide-sm">Tournaments</th><th class="num">'
                'Wagers</th><th class="num hide-sm">Won</th><th class="num">'
                'Cash</th></tr></thead><tbody>%s</tbody></table></div>'
                % (twtourney.money(start_cash()), rows))

    def tourney_record(self, names):
        """This account's personas on the season money list, and their last
        round.  Only the personas that have actually entered something -- a
        table of zeroes says less than a sentence does."""
        esc = lambda v: html.escape(str(v or ''), quote=True)       # noqa: E731
        today = twtourney.today()
        mine = {r['name']: r for r in
                DB.tourney_standings(today - 365, today, twtourney.payout)
                if r['name'] in set(names)}
        if not mine:
            return ('<p class="sub" style="margin:0">No tournament rounds yet. '
                    "Open the CALENDAR on the console and play the day's "
                    'event &mdash; <a href="/tournaments">see the schedule</a>.</p>')
        rows = []
        for who in names:
            r = mine.get(who)
            if not r:
                continue
            last = DB.query('SELECT day FROM tourney WHERE persona = ?'
                            ' ORDER BY day DESC LIMIT 1', (who,))
            when = (twtourney.from_day(last[0]['day']).strftime('%d %b %Y')
                    if last else '&ndash;')
            rows.append('<tr><td><strong>%s</strong></td><td>%s</td><td>%d</td>'
                        '<td>%d</td><td>%s</td><td>%s</td></tr>'
                        % (esc(who), twtourney.money(r['earned']), r['rounds'],
                           r['wins'], _ordinal(r['best']), when))
        table = ('<table><tr><th>Persona</th><th>Earnings</th><th>Events</th>'
                 '<th>Wins</th><th>Best</th><th>Last played</th></tr>'
                 + ''.join(rows) + '</table>')

        # And the rounds themselves, which is where the event and the course
        # actually belong.
        played = []
        for who in names:
            for r in DB.tourney_rounds(who, 10):
                played.append((r['day'], who, r))
        played.sort(key=lambda t: -t[0])
        if played:
            lines = []
            for _day, who, r in played[:12]:
                lines.append(
                    '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                    '<td>%d</td><td>%s</td><td>%s of %d</td></tr>'
                    % (twtourney.from_day(r['day']).strftime('%d %b %Y'),
                       esc(who),
                       '<a class="pl" href="%s">%s</a>' % (
                           event_href(r['day']), esc(r['event']))
                       if r['named']
                       else '<span class="sub">unrecorded</span>',
                       esc(twstats.course_name(r['course'])), r['strokes'],
                       twtourney.to_par(r['fields'], r['par']),
                       _ordinal(r['place']), r['entrants']))
            table += ('<h2>Rounds</h2><table><tr><th>Date</th><th>Persona</th>'
                      '<th>Event</th><th>Course</th><th>Score</th>'
                      '<th>To par</th><th>Finish</th></tr>'
                      + ''.join(lines) + '</table>')
        return table

    def tournaments(self, session):
        """The calendar, the money list and the recent winners.

        Everything here is read from the same tables the game reads, with the
        same payout function -- so a figure on this page and the figure on the
        console's WEEKLY MONEY LEADERS board cannot drift apart.
        """
        esc = lambda v: html.escape(str(v or ''), quote=True)       # noqa: E731
        today = twtourney.today()
        cards = []

        # --- today, and what is coming -------------------------------------
        rows = []
        for e in DB.events(today, 15):
            course = twstats.course_name(e['course'])
            when = twtourney.from_day(e['day'])
            # All four settings, always: in their own columns on a wide
            # screen, and as a line under the event's name on a phone.
            # Today's date goes to its leaderboard; later days have none yet.
            date = when.strftime('%a %d %b')
            if e['day'] == today:
                date = '<a class="pl" href="%s">%s</a>' % (event_href(today), date)
            rows.append('<tr%s><td>%s</td><td><a class="pl" href="#pay-%d">'
                        '<strong>%s</strong></a>'
                        '<div class="show-sm">%s</div></td>'
                        '<td>%s</td>%s<td class="num">%s</td></tr>'
                        % (' class="me"' if e['day'] == today else '',
                           date, e['day'], esc(e['name']),
                           conditions_line(e.get('conditions')),
                           esc(course), conditions_cells(e.get('conditions')),
                           twtourney.money(e['purse'])))
            # The Tour-style split of this event's purse, top ten only --
            # the same `payout` the money list and the console use.
            rows.append('<tr class="payout" id="pay-%d"><td colspan="8">'
                        '<table><caption>%s prize money '
                        '<a href="#schedule">Close</a></caption>'
                        '<thead><tr><th>Place</th><th class="num">Share</th>'
                        '<th class="num">Prize</th></tr></thead><tbody>%s'
                        '</tbody></table></td></tr>'
                        % (e['day'], esc(e['name']), ''.join(
                            '<tr><td>%s</td><td class="num">%s%%</td>'
                            '<td class="num">%s</td></tr>'
                            % (_ordinal(n), ('%.2f' % (share * 100))
                               .rstrip('0').rstrip('.'),
                               twtourney.money(twtourney.payout(e['purse'], n)))
                            for n, share in enumerate(twtourney.PAYOUT, 1))))
        cards.append('<h2 id="schedule">Schedule</h2>' + (
            '<p class="foot" style="margin:-.4rem 0 .8rem">Click an event to '
            'see how its purse is paid out.</p>' if rows else '') + (
            '<table><thead><tr><th>Date</th><th>Event</th><th>Course</th>'
            '<th class="hide-sm">Tees</th><th class="hide-sm">Rough</th>'
            '<th class="hide-sm">Fairways</th><th class="hide-sm">Greens</th>'
            '<th class="num">Purse</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>' if rows else
            '<p class="foot" style="margin:0">No events scheduled.</p>'))

        # --- the season money list -----------------------------------------
        # The money list is THIS SEASON's -- a calendar month.  Past seasons
        # and their champions are on the Hall of Fame page.
        now = twtourney.from_day(today)
        first, _last = twrecords.month_days(now.year, now.month)
        season = DB.tourney_standings(first, today, twtourney.payout)
        rows = []
        for n, r in enumerate(season, 1):
            rows.append('<tr><td class="rank%s">%d</td>'
                        '<td><strong>%s</strong></td><td class="num">%s</td>'
                        '<td class="num">%d</td><td class="num">%d</td>'
                        '<td class="num hide-sm">%s</td></tr>'
                        % (' rank-1' if n == 1 else '', n, plink(r['name']),
                           twtourney.money(r['earned']), r['rounds'], r['wins'],
                           _ordinal(r['best']) if r['best'] else '&ndash;'))
        cards.append('<h2>Money list &middot; %s</h2>'
                     '<p class="foot" style="margin:-.6rem 0 .8rem">This '
                     'season runs to the end of the month. <a class="pl" '
                     'href="/halloffame">Past seasons and champions</a></p>'
                     % now.strftime('%B %Y') + (
            '<table><thead><tr><th>#</th><th>Golfer</th>'
            '<th class="num">Earnings</th><th class="num">Events</th>'
            '<th class="num">Wins</th><th class="num hide-sm">Best</th></tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody></table>' if rows else
            '<p class="foot" style="margin:0">Nobody has won any money yet '
            'this season.</p>'))

        # --- who won what --------------------------------------------------
        rows = []
        for r in DB.tourney_recent(20):
            e, w = r['event'], r['winner']
            # The name the ROUND recorded, not the one the calendar shows now:
            # the calendar is generated, and a round played before that column
            # existed would otherwise be labelled with whatever event that day
            # happens to be today.
            name = w.get('event') or ''
            # What each winner took home: 1st place's share, split evenly when
            # the win was shared.
            purse = e['purse'] if e else 0
            won = (twtourney.money(prize(purse, 1, len(r['winners'])))
                   if purse else '&ndash;')
            rows.append('<tr><td>%s</td><td>%s</td><td class="hide-sm">%s</td>'
                        '<td><strong>%s</strong></td><td class="num hide-sm">%d</td>'
                        '<td class="num hide-sm">%s</td><td class="num">%s</td>'
                        '</tr>'
                        % ('<a class="pl" href="%s">%s</a>' % (
                               event_href(r['day']),
                               twtourney.from_day(r['day']).strftime('%d %b %Y')),
                           '<a class="pl" href="%s">%s</a>' % (
                               event_href(r['day']), esc(name))
                           if name else '<span class="foot">unrecorded</span>',
                           esc(twstats.course_name(w['course'])),
                           ' &amp; '.join(plink(x['name']) for x in r['winners']),
                           w['strokes'],
                           _topar(twtourney.to_par(w['fields'], w['par'])),
                           won))
        if rows:
            cards.append('<h2>Recent winners</h2><table><thead><tr><th>Date</th>'
                         '<th>Event</th><th class="hide-sm">Course</th>'
                         '<th>Winner</th><th class="num hide-sm">Score</th>'
                         '<th class="num hide-sm">To par</th>'
                         '<th class="num">Earnings</th>'
                         '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')

        # Today's event leads this page as well as the front one.  It is the
        # single thing on the site that changes every day, and someone who
        # came straight here for the tournaments should not have to go back to
        # the home page to see what is on right now.
        body = ('<h1>Tournaments</h1><p class="sub">One event a day, the same '
                'course and conditions for everyone, scored on the server. '
                'Each day runs midnight to midnight, %s.</p>'
                '%s%s'
                % (server_tz(), self.today_card(),
                   ''.join('<div class="card">%s</div>' % c for c in cards)))
        return page('Tournaments', body, signed_in=bool(session))

    # -- players, stats, records and courses ---------------------------------
    # All of it comes from twrecords.rounds(): one list of every finished round,
    # head to head and tournament alike, with impossible numbers already
    # thrown out.  The in-game news is built from the same list, so the two can
    # never disagree about who holds a record.

    def not_found(self, title, text, session):
        return self.reply(page(title, '<div class="card"><h2>%s</h2>'
                                      '<p class="sub" style="margin:0">%s</p>'
                                      '</div>' % (html.escape(title), text),
                               signed_in=bool(session)), status=404)

    def player_page(self, name, session):
        rs = twrecords.rounds(DB)
        p = twrecords.player(rs, name)
        if p is None:
            known = DB.persona(name)
            if not known:
                return self.not_found('Golfer', 'Nobody here is called '
                                      '<strong>%s</strong>.' % html.escape(name),
                                      session)
            return self.reply(page(known['name'], (
                '<h1>%s</h1><div class="card"><p class="sub" style="margin:0">'
                'Has not finished a round yet.</p></div>'
                % html.escape(known['name'])), signed_in=bool(session)))
        name = p['name']
        online = any(o['persona'] == name for o in (live_snapshot() or {})
                     .get('online', []))
        best = p['best']
        figs = [_figure(p['rounds'], 'Rounds'),
                _figure('%d&ndash;%d&ndash;%d' % (p['won'], p['lost'], p['tied']),
                        'Head to head'),
                _figure(p['tourney_wins'], 'Tournament wins'),
                _figure(twtourney.money(cash(name)['balance']), 'Cash')]
        if best:
            figs.append(_figure('%d <small>%s</small>' % (
                best['strokes'], twrecords.fmt_par(best['to_par'])), 'Best round'))
        if p['scoring'] is not None:
            figs.append(_figure('%.1f' % p['scoring'], 'Scoring average'))
        if p['longest']:
            figs.append(_figure('%d yd' % p['longest'], 'Longest drive'))
        hcp = twrecords.handicap(rs, name)
        figs.append(_figure(twrecords.fmt_handicap(hcp), 'Handicap'))
        # MY RESUME's head-to-head lines, from the same function the lobby
        # sends the console.
        mine = twrecords.h2h_summary(DB, name)
        figs.append(_figure(mine['rep'], 'Rep'))
        figs.append(_figure(mine['dnf'], 'Did not finish'))

        # Where they stand on each stat table, if they qualify for it.
        standing = {}
        for cat, rows in twrecords.leaders(rs, top=10000):
            for n, (who, _v, _c) in enumerate(rows, 1):
                if who == name:
                    standing[cat[0]] = (n, len(rows))
        stat_rows = []
        for cat in twrecords.CATEGORIES:
            key, title, field, _better, how, _counted, note = cat
            value = p[field]
            if value is None or (field == 'eagles' and not value):
                continue
            rank = standing.get(key)
            stat_rows.append(
                '<tr><td>%s <span class="foot">%s</span></td>'
                '<td class="num">%s</td><td class="num foot">%s</td></tr>'
                % (html.escape(title), html.escape(note),
                   twrecords.fmt(value, how),
                   '%s of %d' % (_ordinal(rank[0]), rank[1]) if rank else ''))
        if p['aces']:
            stat_rows.append('<tr><td>Holes in one</td><td class="num">%d</td>'
                             '<td></td></tr>' % p['aces'])
        if p['longest_putt']:
            stat_rows.append('<tr><td>Longest putt holed</td>'
                             '<td class="num">%d ft</td><td></td></tr>'
                             % p['longest_putt'])

        h2h = ''.join(
            '<tr><td>%s</td><td class="num">%d</td><td class="num">'
            '<a class="pl" href="%s">%d&ndash;%d&ndash;%d</a></td>'
            '<td class="num">%s</td></tr>'
            % (plink(e['opponent']), e['W'] + e['L'] + e['T'],
               h2h_href(name, e['opponent']), e['W'], e['L'], e['T'],
               _date(e['last']))
            for e in p['head_to_head'])
        achieved = twrecords.achievements(rs, name)

        sub_bits = ['Playing since %s' % _date(p['first'])]
        if p['favourite']:
            course, n = p['favourite']
            sub_bits.append('most often at %s (%d)' % (clink(course), n))
        body = ('<h1>%s%s</h1><p class="sub">%s</p>'
                '<div class="card"><h2>Career</h2><div class="figures">%s</div>'
                '</div>'
                % (html.escape(name),
                   ' <span class="tag playing">online now</span>' if online else '',
                   ' &middot; '.join(sub_bits), ''.join(figs)))
        body += self.modes_card(mine)
        body += ('<form method="get" action="/compare" class="vs card">'
                 '<input type="hidden" name="a" value="%s"><div><label>'
                 'Compare with</label><input type="text" name="b" '
                 'list="golfers" required placeholder="another golfer">%s'
                 '</div><button type="submit">Compare</button></form>'
                 % (html.escape(name, quote=True), _golfer_list(rs)))
        body += ('<div class="card"><h2>Achievements &middot; %d of %d</h2>'
                 '<div class="ach">%s</div></div>' % (
                     sum(1 for a in achieved if a['when']), len(achieved),
                     ''.join('<div class="%s"><b>%s</b><span>%s</span>'
                             '<span>%s</span></div>'
                             % ('got' if a['when'] else 'no',
                                html.escape(a['title']), html.escape(a['how']),
                                'Earned %s' % _date(a['when']) if a['when']
                                else html.escape(a['progress']) or 'Not yet')
                             for a in achieved)))
        body += ('<div class="card"><h2>Tour stats</h2><table><thead><tr>'
                 '<th>Stat</th><th class="num">Value</th><th class="num">Rank'
                 '</th></tr></thead><tbody>%s</tbody></table><p class="foot" '
                 'style="margin:.8rem 0 0">Ranks count players with at least %d '
                 'rounds.  <a class="pl" href="/stats">All stat leaders</a></p>'
                 '</div>' % (''.join(stat_rows), twrecords.MIN_ROUNDS))
        if h2h:
            body += ('<div class="card"><h2>Head to head</h2><table><thead><tr>'
                     '<th>Opponent</th><th class="num">Played</th>'
                     '<th class="num">W&ndash;L&ndash;H</th>'
                     '<th class="num">Last played</th></tr></thead><tbody>%s'
                     '</tbody></table></div>' % h2h)
        body += ('<div class="card"><h2>Recent rounds</h2>%s</div>'
                 % _rounds_table(p['recent'], who=False))
        return self.reply(page(name, body, signed_in=bool(session)))

    MODE_LABELS = (('stroke', 'Stroke play'), ('match', 'Match play'),
                   ('mini', '3 Hole Mini-Game'))

    def modes_card(self, mine):
        """Record, rank and games quit in each head-to-head mode, as the
        console's MY RESUME shows them (twrecords.h2h_summary)."""
        rows = []
        for mode, label in self.MODE_LABELS:
            w, l, t = mine['records'][mode]
            # Match play has no halves on MY RESUME: W-L.
            record = ('%d&ndash;%d' % (w, l) if mode == 'match'
                      else '%d&ndash;%d&ndash;%d' % (w, l, t))
            rank = mine['ranks'][mode]
            rows.append('<tr><td>%s</td><td class="num">%s</td>'
                        '<td class="num">%s</td><td class="num">%d</td></tr>'
                        % (label, record, rank if rank else 'N/A',
                           mine['incomplete'][mode]))
        return ('<div class="card"><h2>Head to head &middot; by mode</h2>'
                '<table><thead><tr><th>Mode</th><th class="num">Record</th>'
                '<th class="num">Rank</th><th class="num">Did not finish</th>'
                '</tr></thead><tbody>%s</tbody></table>'
                '<p class="foot" style="margin:.8rem 0 0">As on the '
                'console&rsquo;s MY RESUME. Ranks go by wins, then fewest '
                'losses; Battle games count only toward did-not-finish, as '
                'match play. Did not finish in the last 10 games: '
                '<strong>%d</strong>. Rep is the share of games seen through '
                'rather than quit.</p></div>'
                % (''.join(rows), mine['dnf_last10']))

    def activity_card(self):
        """The last 30 days as two bar charts: rounds and matches played, and
        the most players online at once.  Plain CSS -- the site has no
        script, and a chart this simple does not need one."""
        days = DB.activity_days(30)
        today = days[-1][0]
        busiest = max([r + m for _d, r, m, _p in days] + [1])
        peak = max([p or 0 for _d, _r, _m, p in days] + [1])

        def col(d, bars, tip):
            return ('<div%s title="%s">%s</div>'
                    % (' class="today"' if d == today else '', tip, ''.join(
                        '<i class="%s" style="height:%.1f%%"></i>' % (c, h)
                        for c, h in bars if h)))

        def label(d):
            return twtourney.from_day(d).strftime('%d %b')

        played = ''.join(col(d, (('r', 100.0 * r / busiest),
                                 ('m', 100.0 * m / busiest)),
                             '%s: %d tournament round%s, %d match%s'
                             % (label(d), r, '' if r == 1 else 's',
                                m, '' if m == 1 else 'es'))
                         for d, r, m, _p in days)
        online = ''.join(col(d, (('p', 100.0 * (p or 0) / peak),),
                             '%s: %s' % (label(d), 'no record' if p is None
                                         else '%d online at once' % p))
                         for d, _r, _m, p in days)
        axis = ('<div class="axis"><span>%s</span><span>%s</span>'
                '<span>today</span></div>' % (label(days[0][0]),
                                              label(days[len(days) // 2][0])))
        return ('<div class="card"><h2>The last 30 days</h2>'
                '<p class="key"><i class="r" style="background:var(--gold)"></i>'
                'Tournament rounds <i style="background:var(--c-match)"></i>Matches'
                ' &middot; busiest day %d</p><div class="chart">%s</div>%s'
                '<p class="key" style="margin-top:1.4rem"><i style="background:'
                'var(--c-peak)"></i>Most players online at once &middot; peak %d</p>'
                '<div class="chart" style="height:80px">%s</div>%s</div>'
                % (busiest if any(r + m for _d, r, m, _p in days) else 0,
                   played, axis,
                   peak if any(p for _d, _r, _m, p in days) else 0,
                   online, axis))

    def stats_page(self, session):
        rs = twrecords.rounds(DB)
        cards = []
        for cat, rows in twrecords.leaders(rs):
            _key, title, _field, _better, how, counted, note = cat
            if rows:
                table = ('<table><tbody>%s</tbody></table>' % ''.join(
                    '<tr><td class="rank%s">%d</td><td>%s</td>'
                    '<td class="num">%s</td><td class="num foot">%d rd%s</td>'
                    '</tr>' % (' rank-1' if n == 1 else '', n, plink(who),
                               twrecords.fmt(value, how), c,
                               '' if c == 1 else 's')
                    for n, (who, value, c) in enumerate(rows, 1)))
            elif counted:
                table = ('<p class="foot" style="margin:0">Nobody has played '
                         '%d rounds yet.</p>' % twrecords.MIN_ROUNDS)
            else:
                table = '<p class="foot" style="margin:0">None yet.</p>'
            cards.append('<div class="card"><h2>%s</h2><p class="foot" '
                         'style="margin:-.6rem 0 .6rem">%s</p>%s</div>'
                         % (html.escape(title), html.escape(note), table))
        body = ('<h1>Tour stats</h1><p class="sub">Every finished round on this '
                'server, head to head and tournament.  Averages need at least %d '
                'rounds to qualify; scoring counts 18-hole rounds only, and the '
                'rest are per 18 holes so a Front 9 counts for half.</p>'
                '%s<div class="statgrid">%s</div>'
                % (twrecords.MIN_ROUNDS, self.activity_card(), ''.join(cards)))
        return page('Stats', body, signed_in=bool(session))

    def records_page(self, session):
        rs = twrecords.rounds(DB)
        rec, aces = twrecords.records(rs)
        rows = ''.join(
            '<tr><td>%s</td><td><strong>%s</strong></td><td>%s</td>'
            '<td class="hide-sm">%s</td><td class="num">%s</td></tr>'
            % (html.escape(title), html.escape(show), plink(r['persona']),
               clink(r['course'], r['kind']), _date(r['when']))
            for _key, title, r, show in rec if r)
        body = ('<h1>Records</h1><p class="sub">The best anyone has done on this '
                'server.  A tie goes to whoever did it first.</p>'
                '<div class="card"><h2>Server records</h2>%s</div>'
                % ('<table><thead><tr><th>Record</th><th></th><th>Golfer</th>'
                   '<th class="hide-sm">Course</th><th class="num">Date</th>'
                   '</tr></thead>'
                   '<tbody>%s</tbody></table>' % rows if rows else
                   '<p class="foot" style="margin:0">No rounds finished yet.</p>'))
        course_rows = ''.join(
            '<tr><td>%s</td><td class="num">%s</td><td>%s</td>'
            '<td class="num">%s</td></tr>'
            % (clink(c['course']), _score(c['record']),
               plink(c['record']['persona']), _date(c['record']['when']))
            for c in sorted(twrecords.courses(rs), key=lambda c: c['name'])
            if c['record'])
        if course_rows:
            body += ('<div class="card"><h2>Course records</h2><table><thead>'
                     '<tr><th>Course</th><th class="num">Score</th>'
                     '<th>Golfer</th><th class="num">Date</th></tr></thead>'
                     '<tbody>%s</tbody></table></div>' % course_rows)
        if aces:
            body += ('<div class="card"><h2>Holes in one</h2><table><thead><tr>'
                     '<th>Golfer</th><th>Course</th><th>Round</th>'
                     '<th class="num">Date</th></tr></thead><tbody>%s</tbody>'
                     '</table></div>' % ''.join(
                         '<tr><td>%s</td><td>%s</td><td>%s</td>'
                         '<td class="num">%s</td></tr>'
                         % (plink(r['persona']), clink(r['course'], r['kind']), _kind(r),
                            _date(r['when']))
                         for r in reversed(aces)))
        return page('Records', body, signed_in=bool(session))

    def courses_page(self, session):
        rs = twrecords.rounds(DB)
        cs = twrecords.courses(rs)
        rows = ''.join(
            '<tr><td>%s</td><td class="num">%s</td><td class="num">%d</td>'
            '<td class="num hide-sm">%d</td><td class="num hide-sm">%s</td>'
            '<td class="num">%s</td><td>%s</td></tr>'
            % (clink(c['course']), c['par'] or '&ndash;', c['rounds'],
               c['players'], twrecords.fmt(c['scoring'], '%.1f'),
               _topar(twrecords.fmt(c['to_par'], 'par'))
               if c['to_par'] is not None else '&ndash;',
               '%s by %s' % (_score(c['record']), plink(c['record']['persona']))
               if c['record'] else '')
            for c in cs)
        body = ('<h1>Courses</h1><p class="sub">Every course that has been '
                'played here, hardest first by average score against par.  '
                'Par is learnt from the scorecards themselves.</p>'
                '<div class="card scroll">%s</div>'
                % ('<table><thead><tr><th>Course</th><th class="num">Par</th>'
                   '<th class="num">Rounds</th><th class="num hide-sm">Golfers'
                   '</th><th class="num hide-sm">Average</th>'
                   '<th class="num">To par</th>'
                   '<th>Course record</th></tr></thead><tbody>%s</tbody></table>'
                   % rows if rows else
                   '<p class="foot" style="margin:0">No rounds finished yet.</p>'))
        body += self.conditions_card(rs)
        return page('Courses', body, signed_in=bool(session))

    def conditions_card(self, rs):
        """What each tournament setting does to scores, from the rounds
        themselves -- each round against its own course's average, so a hard
        course does not make its conditions look hard."""
        rows = []
        for label, options in twrecords.conditions_cost(rs, DB):
            cells = ''.join(
                '<td class="num">%s<span class="foot"> (%d)</span></td>'
                % (('<span class="%s">%+.1f</span>'
                    % ('over' if v > 0.05 else 'under' if v < -0.05 else '', v))
                   if v is not None else '&ndash;', n)
                for _o, v, n in options)
            heads = ''.join('<th class="num">%s</th>' % html.escape(o)
                            for o, _v, _n in options)
            rows.append('<tr><th>%s</th>%s</tr><tr><td></td>%s</tr>'
                        % (html.escape(label), heads, cells))
        return ('<div class="card"><h2>What the conditions cost</h2>'
                '<p class="foot" style="margin:-.6rem 0 .8rem">Tournament '
                'rounds only. Each figure is strokes against the average at '
                'the same course, so a hard course doesn&rsquo;t make its '
                'settings look hard: <span class="over">+</span> is harder, '
                '<span class="under">&minus;</span> easier. Rounds counted in '
                'brackets; a figure needs %d.</p><table><tbody>%s</tbody>'
                '</table></div>' % (twrecords.CONDITIONS_MIN_ROUNDS,
                                    ''.join(rows)))

    def course_page(self, index, session):
        try:
            index = int(index)
        except ValueError:
            return self.not_found('Course', 'There is no such course.', session)
        rs = twrecords.rounds(DB)
        c = twrecords.course(rs, index)
        if c is None:
            if 0 <= index < len(twstats.COURSES):
                return self.reply(page(twrecords.course_name(index), (
                    '<h1>%s</h1><div class="card"><p class="sub" '
                    'style="margin:0">Nobody has finished a round here yet.</p>'
                    '</div>' % html.escape(twrecords.course_name(index))),
                    signed_in=bool(session)))
            return self.not_found('Course', 'There is no such course.', session)
        figs = [_figure(c['par'] or '&ndash;', 'Par'),
                _figure(c['rounds'], 'Rounds'),
                _figure(c['players'], 'Golfers')]
        if c['scoring'] is not None:
            figs.append(_figure('%.1f' % c['scoring'], 'Scoring average'))
        if c['to_par'] is not None:
            figs.append(_figure(_topar(twrecords.fmt(c['to_par'], 'par')),
                                'Average to par'))
        if c['gir_pct'] is not None:
            figs.append(_figure('%.0f%%' % c['gir_pct'], 'Greens hit'))
        if c['putts18'] is not None:
            figs.append(_figure('%.1f' % c['putts18'], 'Putts per 18'))
        if c['birdies18'] is not None:
            figs.append(_figure('%.1f' % c['birdies18'], 'Birdies per 18'))
        rec = c['best']
        body = ('<h1>%s</h1><p class="sub">%s</p><div class="card"><h2>The course'
                '</h2><div class="figures">%s</div></div>'
                % (html.escape(c['name']),
                   'Course record <strong>%s</strong> by %s, %s'
                   % (_score(rec), plink(rec['persona']), _date(rec['when']))
                   if rec else 'No full round yet, so no course record.',
                   ''.join(figs)))
        if c['top']:
            body += ('<div class="card"><h2>Best rounds</h2><table><thead><tr>'
                     '<th>#</th><th>Golfer</th><th class="num">Score</th>'
                     '<th>Round</th><th class="num">Date</th></tr></thead>'
                     '<tbody>%s</tbody></table></div>' % ''.join(
                         '<tr><td class="rank%s">%d</td><td>%s</td>'
                         '<td class="num">%s</td><td>%s</td>'
                         '<td class="num">%s</td></tr>'
                         % (' rank-1' if n == 1 else '', n, plink(r['persona']),
                            _score(r), _kind(r), _date(r['when']))
                         for n, r in enumerate(c['top'], 1)))
        body += ('<div class="card"><h2>Recent rounds</h2>%s</div>'
                 % _rounds_table(c['recent'], course=False))
        return self.reply(page(c['name'], body, signed_in=bool(session)))

    # -- comparing two golfers, and the seasons -------------------------------
    def compare_page(self, a, b, session):
        """/compare?a=..&b=..: two golfers' figures side by side, the better
        of each pair picked out."""
        rs = twrecords.rounds(DB)
        form = ('<form method="get" action="/compare" class="vs card">'
                '<div><label>Golfer</label><input type="text" name="a" '
                'value="%s" list="golfers" required></div><div><label>'
                'and</label><input type="text" name="b" value="%s" '
                'list="golfers" required></div><button type="submit">'
                'Compare</button>%s</form>'
                % (html.escape(a, quote=True), html.escape(b, quote=True),
                   _golfer_list(rs)))
        pa = twrecords.player(rs, a) if a else None
        pb = twrecords.player(rs, b) if b else None
        if not (pa and pb):
            missing = [n for n, p in ((a, pa), (b, pb)) if n and not p]
            note = ('<div class="card"><p class="sub" style="margin:0">%s</p>'
                    '</div>' % ('%s has not finished a round here.'
                                % html.escape(missing[0])
                                if missing else 'Pick two golfers.'))
            return page('Compare', '<h1>Compare golfers</h1>' + form + note,
                        signed_in=bool(session))
        a, b = pa['name'], pb['name']
        ca = DB.tourney_career(a, twtourney.payout)
        cb = DB.tourney_career(b, twtourney.payout)
        ha, hb = twrecords.handicap(rs, a), twrecords.handicap(rs, b)
        aa = sum(1 for x in twrecords.achievements(rs, a) if x['when'])
        ab = sum(1 for x in twrecords.achievements(rs, b) if x['when'])

        def best(p):
            return p['best']['strokes'] if p['best'] else None

        # (label, a, b, shown a, shown b, which is better: 'high' / 'low')
        def fig(v, how):
            return twrecords.fmt(v, how) if v is not None else '&ndash;'
        lines = [
            ('Rounds', pa['rounds'], pb['rounds'], None, None, 'high'),
            ('Match record W&ndash;L&ndash;H', pa['won'], pb['won'],
             '%d&ndash;%d&ndash;%d' % (pa['won'], pa['lost'], pa['tied']),
             '%d&ndash;%d&ndash;%d' % (pb['won'], pb['lost'], pb['tied']),
             'high'),
            ('Tournament wins', pa['tourney_wins'], pb['tourney_wins'],
             None, None, 'high'),
            ('Career earnings', ca['earned'], cb['earned'],
             twtourney.money(ca['earned']), twtourney.money(cb['earned']),
             'high'),
            ('Handicap', ha, hb, twrecords.fmt_handicap(ha),
             twrecords.fmt_handicap(hb), 'low'),
            ('Scoring average', pa['scoring'], pb['scoring'],
             fig(pa['scoring'], '%.1f'), fig(pb['scoring'], '%.1f'), 'low'),
            ('Best round', best(pa), best(pb),
             _score(pa['best']) if pa['best'] else '&ndash;',
             _score(pb['best']) if pb['best'] else '&ndash;', 'low'),
            ('Driving distance', pa['drive_avg'], pb['drive_avg'],
             fig(pa['drive_avg'], '%.0f yd'), fig(pb['drive_avg'], '%.0f yd'),
             'high'),
            ('Driving accuracy', pa['fir_pct'], pb['fir_pct'],
             fig(pa['fir_pct'], '%.1f%%'), fig(pb['fir_pct'], '%.1f%%'), 'high'),
            ('Greens in regulation', pa['gir_pct'], pb['gir_pct'],
             fig(pa['gir_pct'], '%.1f%%'), fig(pb['gir_pct'], '%.1f%%'), 'high'),
            ('Putts per 18', pa['putts18'], pb['putts18'],
             fig(pa['putts18'], '%.1f'), fig(pb['putts18'], '%.1f'), 'low'),
            ('Birdies per 18', pa['birdies18'], pb['birdies18'],
             fig(pa['birdies18'], '%.2f'), fig(pb['birdies18'], '%.2f'), 'high'),
            ('Eagles', pa['eagles'], pb['eagles'], None, None, 'high'),
            ('Holes in one', pa['aces'], pb['aces'], None, None, 'high'),
            ('Longest drive', pa['longest'], pb['longest'],
             fig(pa['longest'], '%d yd'), fig(pb['longest'], '%d yd'), 'high'),
            ('Longest putt holed', pa['longest_putt'], pb['longest_putt'],
             fig(pa['longest_putt'], '%d ft'), fig(pb['longest_putt'], '%d ft'),
             'high'),
            ('Achievements', aa, ab, '%d of 5' % aa, '%d of 5' % ab, 'high'),
        ]
        rows = []
        for label, va, vb, sa, sb, how in lines:
            wa = wb = False
            if va is not None and vb is not None and va != vb:
                wa = (va > vb) if how == 'high' else (va < vb)
                wb = not wa
            rows.append('<tr><td>%s</td><td class="num%s">%s</td>'
                        '<td class="num%s">%s</td></tr>'
                        % (label, ' better' if wa else '',
                           sa if sa is not None else va,
                           ' better' if wb else '',
                           sb if sb is not None else vb))
        h = twrecords.head_to_head(rs, a, b)
        met = ('<p class="foot" style="margin:.8rem 0 0">Against each other: '
               '<a class="pl" href="%s">%d&ndash;%d&ndash;%d</a></p>'
               % (h2h_href(a, b), h['W'], h['L'], h['T']) if h else
               '<p class="foot" style="margin:.8rem 0 0">They have not played '
               'each other yet.</p>')
        body = ('<h1>%s <span class="foot" style="font-size:1rem">v</span> %s'
                '</h1>%s<div class="card"><table><thead><tr><th></th>'
                '<th class="num">%s</th><th class="num">%s</th></tr></thead>'
                '<tbody>%s</tbody></table>%s</div>'
                % (plink(a), plink(b), form, html.escape(a), html.escape(b),
                   ''.join(rows), met))
        return page('%s v %s' % (a, b), body, signed_in=bool(session))

    def hall_of_fame(self, session):
        """Every season -- a calendar month -- with its champions, the one in
        progress on top, and the all-time money list with season titles."""
        rs = twrecords.rounds(DB)
        ss = twrecords.seasons(rs, DB)
        titles = collections.Counter(s['champion']['name'] for s in ss
                                     if s['finished'] and s['champion'])

        def month(s):
            return datetime.date(s['year'], s['month'], 1).strftime('%B %Y')

        def cells(s):
            c, w, m, lo = s['champion'], s['most_wins'], s['match'], s['low']
            return ('<td>%s</td><td>%s</td><td class="hide-sm">%s</td>'
                    '<td class="hide-sm">%s</td>' % (
                        '%s <span class="foot">%s</span>' % (
                            plink(c['name']), twtourney.money(c['earned']))
                        if c else '&ndash;',
                        '%s <span class="foot">%d</span>' % (
                            plink(w['name']), w['wins']) if w else '&ndash;',
                        '%s <span class="foot">%d&ndash;%d&ndash;%d</span>' % (
                            plink(m['persona']), m['W'], m['L'], m['T'])
                        if m else '&ndash;',
                        '%s <span class="foot">%s</span>' % (
                            plink(lo['persona']), _score(lo)) if lo
                        else '&ndash;'))

        head = ('<thead><tr><th>Season</th><th>Money champion</th>'
                '<th>Most wins</th><th class="hide-sm">Match play</th>'
                '<th class="hide-sm">Low round</th></tr></thead>')
        now = [s for s in ss if not s['finished']]
        past = [s for s in ss if s['finished']]
        cards = []
        if now:
            s = now[0]
            last = twtourney.from_day(s['last'])
            cards.append(
                '<div class="card"><h2>This season &middot; %s</h2>'
                '<p class="foot" style="margin:-.6rem 0 .8rem">Ends %s. Leaders '
                'so far; today&rsquo;s event counts once it closes.</p>'
                '<table>%s<tbody><tr><td>so far</td>%s</tr></tbody></table>'
                '</div>' % (month(s), last.strftime('%A %d %B'), head,
                            cells(s)))
        cards.append(
            '<div class="card"><h2>Past seasons</h2>%s</div>'
            % ('<table>%s<tbody>%s</tbody></table>' % (head, ''.join(
                '<tr><td>%s</td>%s</tr>' % (month(s), cells(s)) for s in past))
               if past else '<p class="foot" style="margin:0">The first season '
               'is still being played.</p>'))
        first = DB.one('SELECT MIN(day) AS d FROM tourney')
        alltime = (DB.tourney_standings(first['d'], twtourney.today(),
                                        twtourney.payout)
                   if first and first['d'] is not None else [])
        rows = ''.join(
            '<tr><td class="rank%s">%d</td><td><strong>%s</strong></td>'
            '<td class="num">%s</td><td class="num">%d</td>'
            '<td class="num">%d</td></tr>'
            % (' rank-1' if n == 1 else '', n, plink(r['name']),
               twtourney.money(r['earned']), r['wins'], titles.get(r['name'], 0))
            for n, r in enumerate([r for r in alltime if r['earned']][:20], 1))
        cards.append(
            '<div class="card"><h2>All-time money list</h2>%s</div>'
            % ('<table><thead><tr><th>#</th><th>Golfer</th>'
               '<th class="num">Earnings</th><th class="num">Wins</th>'
               '<th class="num">Titles</th></tr></thead><tbody>%s</tbody>'
               '</table>' % rows if rows else
               '<p class="foot" style="margin:0">No prize money won yet.</p>'))
        body = ('<h1>Hall of Fame</h1><p class="sub">A season is a calendar '
                'month. Its money champion is whoever earns the most in that '
                'month&rsquo;s tournaments; a title is a season won.</p>'
                + ''.join(cards))
        return page('Hall of Fame', body, signed_in=bool(session))

    # -- one event, and two players -----------------------------------------
    def event_page(self, date, session):
        """/event/YYYY-MM-DD: an event's details and its whole field, with
        each player's place and prize.  Today's is live; a future one shows
        what is coming."""
        try:
            when = datetime.date.fromisoformat(date)
        except ValueError:
            return self.not_found('Event', 'That is not a date.', session)
        day, today = twtourney.to_day(when), twtourney.today()
        event = DB.event(day)
        board = DB.tourney_day(day, limit=1000)
        if not event and not board:
            return self.not_found('Event', 'There was no event on %s.'
                                  % when.strftime('%d %B %Y'), session)
        name = ((board[0]['event'] if board else '') or
                (event['name'] if event else 'Online Tournament'))
        course = event['course'] if event else board[0]['course']
        purse = event['purse'] if event else 0

        state = ('<span class="tag playing">in progress &middot; closes in %s'
                 ' (midnight %s)</span>' % (closes_in(), server_tz())
                 if day == today else
                 '<span class="tag">not played yet</span>' if day > today else
                 '<span class="tag">final</span>')
        # Only to days that had an event -- the first one has no yesterday.
        links = []
        if DB.event(day - 1) or DB.tourney_day(day - 1, limit=1):
            links.append('<a class="pl" href="%s">&larr; %s</a>' % (
                event_href(day - 1),
                twtourney.from_day(day - 1).strftime('%d %b')))
        if day < today and (DB.event(day + 1) or DB.tourney_day(day + 1, limit=1)):
            links.append('<a class="pl" href="%s">%s &rarr;</a>' % (
                event_href(day + 1),
                twtourney.from_day(day + 1).strftime('%d %b')))
        nav = ('<p class="foot" style="margin:0 0 1rem">%s</p>'
               % ' &middot; '.join(links) if links else '')
        figs = [_figure(twtourney.money(purse) if purse else '&ndash;', 'Purse'),
                _figure(len(board), 'Entrants')]
        if board:
            figs.append(_figure(board[0]['strokes'], 'Winning score'
                                if day < today else 'Leading score'))
        body = ('%s<h1>%s %s</h1><p class="sub">%s &middot; %s</p>'
                '<div class="card"><p style="margin:0 0 1rem">%s</p>'
                '<div class="figures">%s</div></div>'
                % (nav, html.escape(name), state, clink(course),
                   when.strftime('%A %d %B %Y'),
                   conditions_line(event.get('conditions') if event else None),
                   ''.join(figs)))
        if board:
            rows = ''.join(
                '<tr%s><td class="rank%s">%s</td><td>%s</td>'
                '<td class="num">%d</td><td class="num">%s</td>'
                '<td class="num">%s</td></tr>'
                % (' class="me"' if r['place'] == 1 else '',
                   ' rank-1' if r['place'] == 1 else '', _place(r),
                   plink(r['name']), r['strokes'],
                   _topar(twtourney.to_par(r['fields'], r['par'])),
                   twtourney.money(prize(purse, r['place'], r['tied']))
                   if purse else '&ndash;')
                for r in board)
            body += ('<div class="card"><h2>%s</h2><table><thead><tr>'
                     '<th>Pos</th><th>Golfer</th><th class="num">Score</th>'
                     '<th class="num">To par</th><th class="num">Prize</th>'
                     '</tr></thead><tbody>%s</tbody></table>%s</div>'
                     % ('Leaderboard' if day >= today else 'Final leaderboard',
                        rows,
                        '<p class="foot" style="margin:.8rem 0 0">Still open, so '
                        'places and prizes can change until midnight.</p>'
                        if day == today else ''))
        elif day <= today:
            body += ('<div class="card"><p class="sub" style="margin:0">Nobody '
                     'posted a score%s.</p></div>'
                     % (' yet' if day == today else ''))
        return self.reply(page(name, body, signed_in=bool(session)))

    def h2h_page(self, a, b, session):
        """/h2h/<a>/<b>: two players' record against each other."""
        rs = twrecords.rounds(DB)
        h = twrecords.head_to_head(rs, a, b)
        if h is None:
            pa, pb = DB.persona(a), DB.persona(b)
            if not pa or not pb:
                return self.not_found('Head to head', 'Nobody here is called '
                                      '<strong>%s</strong>.' % html.escape(
                                          a if not pa else b), session)
            return self.reply(page('%s v %s' % (pa['name'], pb['name']), (
                '<h1>%s v %s</h1><div class="card"><p class="sub" '
                'style="margin:0">They have not finished a match against each '
                'other yet.</p></div>' % (plink(pa['name']), plink(pb['name']))),
                signed_in=bool(session)))
        a, b = h['a'], h['b']
        figs = [_figure(h['W'], '%s wins' % html.escape(a)),
                _figure(h['T'], 'Halved'),
                _figure(h['L'], '%s wins' % html.escape(b))]
        kinds = ' &middot; '.join(
            '%s %d&ndash;%d&ndash;%d' % (label, t['W'], t['L'], t['T'])
            for kind, label in twrecords.KIND_NAMES.items()
            for t in [h['kinds'].get(kind)] if t)

        def score(r):
            return _score(r) if r else '&ndash;'

        rows = ''.join(
            '<tr><td>%s</td><td class="hide-sm">%s</td><td class="hide-sm">%s'
            '</td><td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>'
            % (_date(m['when']), _kind(m),
               clink(m['course'], m['kind']), score(m['mine']), score(m['theirs']),
               '%s won' % html.escape(a) if m['result'] == 'W' else
               '%s won' % html.escape(b) if m['result'] == 'L' else 'Halved')
            for m in h['meetings'][:10])
        ha, hb = twrecords.handicap(rs, a), twrecords.handicap(rs, b)
        given = twrecords.strokes_given(ha, hb)
        net = ('<p class="foot" style="margin:1rem 0 0">Handicaps: %s %s, '
               '%s %s. %s</p>' % (
                   html.escape(a), twrecords.fmt_handicap(ha), html.escape(b),
                   twrecords.fmt_handicap(hb),
                   'In a net game they play level.' if given and not given[1]
                   else 'In a net game %s gives %s %d stroke%s a round.' % (
                       html.escape(a if given[0] else b),
                       html.escape(b if given[0] else a), given[1],
                       '' if given[1] == 1 else 's') if given else
                   'Both need 3 full rounds for a net game.'))
        body = ('<h1>%s <span class="foot" style="font-size:1rem">v</span> %s</h1>'
                '<p class="sub">%d meeting%s%s &middot; <a class="pl" href="%s">'
                'see it from %s&rsquo;s side</a> &middot; <a class="pl" '
                'href="/compare?a=%s&amp;b=%s">compare their stats</a></p>'
                '<div class="card"><h2>Record</h2><div class="figures">%s</div>'
                '%s</div><div class="card"><h2>Last meetings</h2><table><thead><tr>'
                '<th>Date</th><th class="hide-sm">Round</th>'
                '<th class="hide-sm">Course</th><th class="num">%s</th><th class="num">%s</th><th>Result</th>'
                '</tr></thead><tbody>%s</tbody></table></div>'
                % (plink(a), plink(b), len(h['meetings']),
                   '' if len(h['meetings']) == 1 else 's',
                   (' &middot; ' + kinds) if kinds else '', h2h_href(b, a),
                   html.escape(b), urllib.parse.quote(a, safe=''),
                   urllib.parse.quote(b, safe=''), ''.join(figs), net,
                   html.escape(a),
                   html.escape(b), rows))
        return self.reply(page('%s v %s' % (a, b), body, signed_in=bool(session)))

    # -- the operator's admin page ------------------------------------------
    @staticmethod
    def admin_allowed(key):
        return bool(ADMIN_KEY) and secrets.compare_digest(
            key.encode('utf-8'), ADMIN_KEY.encode('utf-8'))

    @staticmethod
    def news_file_text():
        try:
            with open(NEWS_FILE, encoding='utf-8') as f:
                return f.read()
        except OSError:
            return ''

    def do_admin(self, action, fields):
        """Carry out one admin form and show the page again with the result.

        No redirect: a reset password is shown once, in the page itself, and
        must never travel in a URL where logs and browser history keep it.
        """
        query = fields.get('q', '')
        try:
            if action == 'password':
                acct = DB.one('SELECT * FROM accounts WHERE id = ?',
                              (int(fields.get('id') or 0),))
                if not acct:
                    raise twdb.Error('no such account')
                new = fields.get('password', '').strip() or new_password()
                DB.set_password(acct['id'], new)
                note = ('%s\'s password is now %s -- give it to them, then '
                        'they can change it on their account page'
                        % (acct['name'], new))
            elif action == 'ban':
                acct = DB.one('SELECT * FROM accounts WHERE id = ?',
                              (int(fields.get('id') or 0),))
                if not acct:
                    raise twdb.Error('no such account')
                ban = fields.get('ban') == '1'
                DB.set_disabled(acct['id'], ban)
                note = ('banned %s -- they cannot sign in, on the console or '
                        'here; anyone already in the lobby stays until they '
                        'leave' % acct['name'] if ban else
                        'lifted the ban on %s' % acct['name'])
            elif action == 'rename':
                old = fields.get('persona', '')
                if any(o['persona'].lower() == old.lower()
                       for o in DB.online()):
                    raise twdb.Error('%s is online right now -- rename them '
                                     'once they have signed off' % old)
                new = DB.rename_persona(old, fields.get('name', ''))
                note = 'renamed %s to %s, with all their results' % (old, new)
                query = query or new
            elif action == 'delete':
                acct = DB.one('SELECT * FROM accounts WHERE id = ?',
                              (int(fields.get('id') or 0),))
                if not acct:
                    raise twdb.Error('no such account')
                if fields.get('confirm', '').strip().lower() != \
                        acct['name'].lower():
                    raise twdb.Error('to delete %s, type the account name '
                                     'exactly' % acct['name'])
                online = {o['persona'].lower() for o in DB.online()}
                busy = [p for p in DB.personas(acct['id'])
                        if p.lower() in online]
                if busy:
                    raise twdb.Error('%s is online right now -- delete the '
                                     'account once they have signed off'
                                     % ', '.join(busy))
                gone = DB.delete_account(acct['id'])
                # Sign the account out of the web site too.
                with SESSION_LOCK:
                    for token in [t for t, d in SESSIONS.items()
                                  if d.get('account') == acct['id']]:
                        del SESSIONS[token]
                note = ('deleted %s (%s): %d tournament round%s and %d '
                        'match%s removed; abuse reports about them are kept'
                        % (gone['account'], ', '.join(gone['personas']) or
                           'no personas', gone['rounds'],
                           '' if gone['rounds'] == 1 else 's', gone['matches'],
                           '' if gone['matches'] == 1 else 'es'))
                query = ''
            elif action == 'news':
                text = fields.get('news', '').replace('\r\n', '\n')
                if len(text) > NEWS_LIMIT:
                    raise twdb.Error('the news is %d characters; the limit is %d'
                                     % (len(text), NEWS_LIMIT))
                if any(ord(c) > 126 or (ord(c) < 32 and c != '\n')
                       for c in text):
                    raise twdb.Error('the PS2 can only show plain ASCII -- '
                                     'take out accents, curly quotes and emoji')
                with open(NEWS_FILE, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(text)
                note = 'news saved -- the next player to open NEWS sees it'
            else:
                return self.reply(page('Not found', '<h1>Not found</h1>'),
                                  status=404)
        except (twdb.Error, ValueError, OSError) as exc:
            return self.reply(self.admin_page(query, str(exc), kind='err',
                                              news=fields.get('news')),
                              headers=PRIVATE)
        LOG.write('%s admin %s: %s' % (time.strftime('%H:%M:%S'), action,
                                       note if action != 'password'
                                       else 'reset for %s' % acct['name']))
        return self.reply(self.admin_page(query, note, kind='ok'),
                          headers=PRIVATE)

    def admin_page(self, query='', note=None, kind='ok', news=None):
        """Find an account, then reset its password, ban it or rename one of
        its personas; and edit the in-game news."""
        base = '/admin/%s' % ADMIN_KEY
        esc = lambda v: html.escape(str(v or ''), quote=True)      # noqa: E731
        online = {o['persona'].lower() for o in DB.online()}
        found = DB.find_accounts(query) if query.strip() else []

        accts = []
        for a in found:
            personas = ''.join(
                '<form method="post" action="%s/rename"><span class="lbl">'
                'Persona <strong style="color:var(--ink)">%s</strong>%s</span>'
                '<input type="hidden" name="q" value="%s">'
                '<input type="hidden" name="persona" value="%s">'
                '<input type="text" name="name" placeholder="new name" '
                'maxlength="%d" required><button class="quiet" type="submit">'
                'Rename</button></form>'
                % (base, esc(p), ' <span class="tag playing">online</span>'
                   if p.lower() in online else '', esc(query), esc(p),
                   twdb.MAX_NAME)
                for p in a['personas'])
            accts.append(
                '<div class="admin-acct"><p style="margin:0"><strong>%s</strong>'
                '%s <span class="foot">&middot; %s &middot; joined %s &middot; '
                '%s</span></p>%s'
                '<form method="post" action="%s/password"><span class="lbl">'
                'Password</span><input type="hidden" name="q" value="%s">'
                '<input type="hidden" name="id" value="%d">'
                '<input type="text" name="password" placeholder="blank makes '
                'one up" maxlength="%d" autocomplete="off">'
                '<button type="submit">Reset</button></form>'
                '<form method="post" action="%s/ban"><span class="lbl">Access'
                '</span><input type="hidden" name="q" value="%s">'
                '<input type="hidden" name="id" value="%d">'
                '<input type="hidden" name="ban" value="%s">'
                '<button class="quiet" type="submit">%s</button></form>'
                '<form method="post" action="%s/delete"><span class="lbl">'
                'Delete</span><input type="hidden" name="q" value="%s">'
                '<input type="hidden" name="id" value="%d">'
                '<input type="text" name="confirm" autocomplete="off" '
                'placeholder="type %s to confirm" required>'
                '<button class="danger" type="submit">Delete account</button>'
                '</form></div>'
                % (esc(a['name']), ' <span class="tag bad">banned</span>'
                   if a['disabled'] else '', esc(a['mail']) or 'no email',
                   _date(a['created']),
                   'last signed in %s' % _date(a['last_seen'])
                   if a['last_seen'] else 'never signed in',
                   personas, base, esc(query), a['id'], twdb.MAX_PASSWORD,
                   base, esc(query), a['id'], '0' if a['disabled'] else '1',
                   'Lift ban' if a['disabled'] else 'Ban account',
                   base, esc(query), a['id'], esc(a['name'])))
        results = (''.join(accts) if accts else
                   '<p class="foot" style="margin:1rem 0 0">No account or '
                   'persona matches that.</p>' if query.strip() else '')

        text = self.news_file_text() if news is None else news
        body = ('<h1>Admin</h1><p class="sub">Not linked from anywhere &mdash; '
                'keep this address to yourself.</p>'
                '<div class="card"><h2>Accounts</h2>'
                '<form method="get" action="%s" class="row" style="align-items:'
                'flex-end"><div style="flex:3 1 14rem"><label>Account or persona'
                '</label><input type="text" name="q" value="%s" autofocus></div>'
                '<div style="flex:0 0 auto"><button type="submit" style="margin:0">'
                'Find</button></div></form>'
                '<p class="foot" style="margin:.8rem 0 1.2rem">A new password '
                'must be %d&ndash;%d characters. A ban blocks the whole account, '
                'all its personas. Renaming carries the persona&rsquo;s results, '
                'rounds and buddies with it; do it while they are offline. '
                'Deleting removes the account, its personas and everything '
                'they played, for good &mdash; only abuse reports are kept.</p>'
                '%s</div>'
                '<div class="card"><h2>In-game news</h2>'
                '<form method="post" action="%s/news"><textarea name="news" '
                'maxlength="%d" spellcheck="true">%s</textarea>'
                '<p class="foot" style="margin:.5rem 0 0">Plain ASCII, up to %d '
                'characters. The NEWS screen fits about %d characters a line; '
                'longer lines are wrapped for you. TW05&rsquo;s news screen '
                'can&rsquo;t draw a hyphen (-), so leave them out. The '
                'server&rsquo;s automatic digest of recent results follows it. '
                'Saved to '
                '<code>%s</code>.</p><button type="submit">Save news</button>'
                '</form></div>'
                % (base, esc(query), twdb.MIN_PASSWORD, twdb.MAX_PASSWORD,
                   results, base, NEWS_LIMIT, html.escape(text), NEWS_LIMIT,
                   twrecords.NEWS_WIDTH,
                   esc(NEWS_FILE)))
        return page('Admin', body, message=note, kind=kind, stats=False)

    # -- the operator's abuse reports ---------------------------------------
    @staticmethod
    def reports_allowed(key):
        return bool(REPORTS_KEY) and secrets.compare_digest(
            key.encode('utf-8'), REPORTS_KEY.encode('utf-8'))

    def do_report_handled(self, key, fields):
        try:
            rid = int(fields.get('id', ''))
        except ValueError:
            return self.redirect(u('/reports/%s' % key))
        reopen = fields.get('action') == 'reopen'
        DB.set_report_handled(rid, not reopen)
        return self.redirect(u('/reports/%s?m=%s' % (key, urllib.parse.quote(
            'report #%d %s' % (rid, 'reopened' if reopen else 'marked handled')))))

    def reports_page(self, note=None):
        """Every REPORT ABUSE from the game, with the chat attached to it.

        Open reports in full; handled ones as a short list underneath, in
        case one needs reopening.  Acting on a report is done from the shell
        (`twdb.py --disable ACCOUNT`) -- this page only reads and files.
        """
        # Bare path: page() puts the mount point on every action= itself.
        action = '/reports/%s/handle' % REPORTS_KEY
        open_ = DB.reports(handled=False)
        done = DB.reports(handled=True, limit=30)

        def when(t):
            return time.strftime('%d %b %H:%M', time.localtime(t))

        def who(r):
            if not r['account']:
                return ('<strong>%s</strong> <span class="tag bad">no such '
                        'persona now</span>' % html.escape(r['accused']))
            flag = ('<span class="tag bad">disabled</span>'
                    if r['disabled'] else '')
            return ('<strong>%s</strong> (account %s) %s'
                    % (html.escape(r['accused']), html.escape(r['account']),
                       flag))

        cards = ['<div class="card"><h2>Abuse reports</h2>'
                 '<h1 style="margin:0">%d open</h1>'
                 '<p class="sub" style="margin:.4rem 0 0">Filed with REPORT '
                 'ABUSE in the game. The chat is what this server relayed in '
                 'the hour before: what the reported player said in rooms, and '
                 'private or EA Messenger lines between the two. To act on one, '
                 'on the server: <code>python3 twdb.py --disable ACCOUNT</code>.'
                 ' This page is not linked from anywhere &mdash; keep its '
                 'address to yourself.</p></div>' % len(open_)]

        for r in open_:
            lines = ''.join(
                '<li><time>%s</time><span class="tag">%s</span>'
                '<span class="who">%s%s</span><span class="said">%s</span></li>'
                % (time.strftime('%H:%M:%S', time.localtime(c.get('at', 0))),
                   html.escape(c.get('via', '')),
                   html.escape(c.get('from', '')),
                   (' &rarr; %s' % html.escape(c['to'])) if c.get('to') else '',
                   html.escape(c.get('text', '')))
                for c in r['chat'])
            chat = ('<ul class="chat">%s</ul>' % lines if lines else
                    '<p class="sub" style="margin:.8rem 0 0">No chat from them '
                    'was relayed in the hour before this report.</p>')
            cards.append(
                '<div class="card"><h2>#%d &middot; %s</h2>'
                '<p style="margin:0">%s reported by <strong>%s</strong>%s'
                ' &middot; %d report%s name them</p>%s'
                '<form method="post" action="%s" class="inline">'
                '<input type="hidden" name="id" value="%d">'
                '<button type="submit" style="margin-top:1rem">Mark handled'
                '</button></form></div>'
                % (r['id'], when(r['at']), who(r), html.escape(r['reporter']),
                   (' in %s' % html.escape(r['room'])) if r['room'] else '',
                   r['against'], '' if r['against'] == 1 else 's', chat,
                   action, r['id']))

        if done:
            rows = ''.join(
                '<tr><td>#%d</td><td>%s</td><td>%s &rarr; <strong>%s</strong>'
                '</td><td class="num">%d</td><td><form method="post" '
                'action="%s" class="inline"><input type="hidden" name="id" '
                'value="%d"><input type="hidden" name="action" value="reopen">'
                '<button class="quiet" type="submit">reopen</button></form>'
                '</td></tr>'
                % (r['id'], when(r['at']), html.escape(r['reporter']),
                   html.escape(r['accused']), len(r['chat']), action, r['id'])
                for r in done)
            cards.append('<div class="card"><h2>Handled</h2><table><thead><tr>'
                         '<th></th><th>Filed</th><th>Report</th>'
                         '<th class="num">Chat lines</th><th></th></tr></thead>'
                         '<tbody>%s</tbody></table></div>' % rows)
        return page('Reports', ''.join(cards), message=note, kind='ok',
                    stats=False)

    def profile(self, session, note=None, kind='err'):
        account = DB.one('SELECT * FROM accounts WHERE id = ?',
                         (session['account'],))
        personas = DB.personas(account['id'])
        csrf = html.escape(session['csrf'], quote=True)
        esc = lambda v: html.escape(str(v or ''), quote=True)       # noqa: E731

        chips = []
        for name in personas:
            played, won, lost, tied = DB.record(name)
            drop = ('<form class="inline" method="post" action="/persona/drop">'
                    '<input type="hidden" name="csrf" value="%s">'
                    '<input type="hidden" name="persona" value="%s">'
                    '<button class="quiet" type="submit">remove</button></form>'
                    % (csrf, esc(name))) if len(personas) > 1 else ''
            chips.append('<tr><td><strong>%s</strong> <a class="pl" '
                         'href="/player/%s" style="font-size:.8rem">'
                         'public page</a></td><td>%d played</td>'
                         '<td>%d&ndash;%d&ndash;%d</td>'
                         '<td class="num">%s</td>'
                         '<td style="text-align:right">%s</td></tr>'
                         % (esc(name), urllib.parse.quote(name, safe=''),
                            played, won, lost, tied,
                            twtourney.money(cash(name)['balance']), drop))

        rows = []
        for name in personas:
            for m in DB.matches(name, 10):
                mine = next(p for p in m['players'] if p['name'] == name)
                them = next(p for p in m['players'] if p['name'] != name)
                verdict = ('tie' if m['winner'] is None else
                           'won' if m['winner'] == name else 'lost')
                setup = m.get('setup') or {}
                kind = twrecords.match_kind(m)
                course = ('3 random holes' if kind == 'mini' else
                          twstats.course_name(setup.get('COUR')))
                mode = twrecords.KIND_NAMES.get(kind, kind)
                extra = []
                try:
                    wager = int(setup.get('WAGER') or 0)
                except ValueError:
                    wager = 0
                if wager:
                    extra.append('%s wager' % twtourney.money(wager))
                rows.append(
                    '<tr><td>%s</td><td>%s</td><td>%s</td><td>%d&ndash;%d</td>'
                    '<td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>'
                    % (esc(name), plink(them['name']), verdict,
                       mine['strokes'], them['strokes'],
                       esc(course) or '&mdash;', mode, mine['holes'],
                       time.strftime('%Y-%m-%d %H:%M',
                                     time.localtime(m['received']))))
                if extra:
                    rows.append('<tr><td></td><td colspan="7" class="sub" '
                                'style="margin:0;font-size:.8rem">%s</td></tr>'
                                % ' &middot; '.join(extra))
        history = ('<table><tr><th>Persona</th><th>Opponent</th><th></th>'
                   '<th>Strokes</th><th>Course</th><th>Mode</th><th>Holes</th>'
                   '<th>Played</th></tr>'
                   + ''.join(rows) + '</table>') if rows else (
            '<p class="sub" style="margin:0">No matches reported yet.</p>')

        add = ''
        if len(personas) < twdb.MAX_PERSONAS:
            add = """
<form method="post" action="/persona/add">
  <input type="hidden" name="csrf" value="%s">
  <label>Add a persona (%d of %d used)</label>
  <input type="text" name="persona" maxlength="%d" required>
  <button type="submit">Add persona</button>
</form>""" % (csrf, len(personas), twdb.MAX_PERSONAS, twdb.MAX_NAME)
        else:
            add = ('<p class="sub" style="margin:.5rem 0 0">All %d slots are in '
                   'use &mdash; the game only reads four from the login reply.</p>'
                   % twdb.MAX_PERSONAS)

        born = esc(account['born'])
        born_value = ('%s-%s-%s' % (born[:4], born[4:6], born[6:8])
                      if len(born) == 8 and born.isdigit() else '')
        body = """
<h1>%s</h1>
<p class="sub">Signed in &mdash; <a href="/leaderboard">leaderboard</a>
 &middot; <a href="/tournaments">tournaments</a>
 &middot; <a href="/logout">sign out</a></p>

<div class="card">
<h2>Personas</h2>
<table>%s</table>
%s
</div>

%s

<div class="card">
<h2>Profile</h2>
<form method="post" action="/profile">
  <input type="hidden" name="csrf" value="%s">
  <div class="row">
    <div><label>Email</label><input type="email" name="mail" value="%s"></div>
    <div><label>Gender</label><select name="gend">
      <option value="M"%s>Male</option><option value="F"%s>Female</option>
    </select></div>
    <div><label>Date of birth</label>
      <input type="text" name="born" placeholder="YYYY-MM-DD" value="%s"></div>
  </div>
  <label><input type="checkbox" name="spam" %s style="width:auto"> Happy to
    receive news</label>
  <button type="submit">Save profile</button>
</form>
</div>

<div class="card">
<h2>Password</h2>
<form method="post" action="/password">
  <input type="hidden" name="csrf" value="%s">
  <div class="row">
    <div><label>Current password</label>
      <input type="password" name="current" required></div>
    <div><label>New password</label>
      <input type="password" name="password" required></div>
    <div><label>Repeat new password</label>
      <input type="password" name="password2" required></div>
  </div>
  <button type="submit">Change password</button>
</form>
</div>

<div class="card">
<h2>Tournaments</h2>
%s
</div>

<div class="card">
<h2>Recent matches</h2>
%s
</div>
""" % (esc(account['name']), ''.join(chips), add, self.cash_card(personas),
       csrf, esc(account['mail']),
       ' selected' if account['gend'] != 'F' else '',
       ' selected' if account['gend'] == 'F' else '',
       born_value, 'checked' if account['spam'] else '', csrf,
       self.tourney_record(personas), history)
        return page(esc(account['name']), body, note, kind, signed_in=True)

    def cash_card(self, names):
        """Each persona's cash and where it came from, and the last few
        wagers and spends.  The balance is what the console shows as
        ONLINE EARNINGS on MY RESUME, and what it lets you wager."""
        esc = lambda v: html.escape(str(v or ''), quote=True)       # noqa: E731
        rows, moves = [], []
        for who in names:
            c = cash(who)
            rows.append(
                '<tr><td><strong>%s</strong></td><td class="num hide-sm">%s</td>'
                '<td class="num">%s</td><td class="num">%s</td>'
                '<td class="num hide-sm">%s</td><td class="num"><strong>%s'
                '</strong></td></tr>'
                % (esc(who), twtourney.money(c['start']),
                   twtourney.money(c['tourney']) if c['tourney'] else '&ndash;',
                   _signed(c['earned'] - c['lost']) if c['wagers'] else '&ndash;',
                   _signed(-c['spent']) if c['spent'] else '&ndash;',
                   twtourney.money(c['balance'])))
            games = ({m['auth']: m for m in DB.matches(who, limit=None)}
                     if c['history'] else {})
            for h in c['history']:
                m = games.get(h['ref'])
                if h['kind'] == 'wager':
                    what = 'Wager'
                    if m:
                        them = next((p['name'] for p in m['players']
                                     if p['name'] != who), '')
                        what = '%s wager v %s' % (
                            html.escape(twrecords.KIND_NAMES.get(
                                twrecords.match_kind(m), 'Match')), plink(them))
                    if not h['amount']:
                        what += ' <span class="foot">(tied, stakes back)</span>'
                else:
                    what = 'Spent in the game'
                moves.append((h['at'], esc(who), what, h['amount']))
        moves.sort(key=lambda t: -t[0])
        table = ('<table><thead><tr><th>Persona</th><th class="num hide-sm">'
                 'Start</th><th class="num">Tournaments</th><th class="num">'
                 'Wagers</th><th class="num hide-sm">Spent</th>'
                 '<th class="num">Cash</th></tr></thead><tbody>%s</tbody>'
                 '</table>' % ''.join(rows))
        if moves:
            table += ('<h3>Wagers and spending</h3><table><tbody>%s</tbody>'
                      '</table>' % ''.join(
                          '<tr><td>%s</td><td>%s</td><td>%s</td>'
                          '<td class="num">%s</td></tr>'
                          % (_date(at), who, what, _signed(amount))
                          for at, who, what, amount in moves[:12]))
        return ('<div class="card"><h2>Cash</h2><p class="foot" style="margin:'
                '-.4rem 0 .8rem">Every golfer starts with %s. Tournament prize '
                'money adds to it; a wager puts both stakes in the pot and the '
                'winner takes it. This is ONLINE EARNINGS on the console&rsquo;s '
                'MY RESUME.</p>%s</div>' % (twtourney.money(start_cash()), table))


def _signed(n):
    """+$6,000 in green, -$3,000 in red, $0 plain."""
    if not n:
        return '$0'
    return '<span class="%s">%s%s</span>' % (
        'plus' if n > 0 else 'minus', '+' if n > 0 else '&minus;',
        twtourney.money(abs(n)))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    global DB, BASE, TRUST_PROXY, SECURE_COOKIE, ADVERTISE, LOBBY_PORT
    global REPORTS_KEY, ADMIN_KEY, NEWS_FILE, LOG
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=twdb.DEFAULT_DB)
    ap.add_argument('--host', default='0.0.0.0')
    # 8081: TW04's site has 8080, and the two may well share a machine.
    ap.add_argument('--port', type=int, default=8081)
    ap.add_argument('--base-path', default='',
                    help='mount the site under a path, e.g. /TW05Online, when '
                         'it sits behind a reverse proxy that is not giving it '
                         'a hostname of its own')
    ap.add_argument('--no-secure-cookie', dest='secure_cookie',
                    action='store_false',
                    help='do not mark the session cookie Secure.  Only for '
                         'testing over plain HTTP; it is on by default because '
                         'anything public should be behind TLS')
    ap.add_argument('--advertise', default='',
                    help='the address players should point their game at, as a '
                         'name or an IPv4 address.  Only needed when the lobby '
                         'is not on the same host as this site, or when this '
                         'host cannot resolve its own public name -- otherwise '
                         'the address the request arrived at is used')
    ap.add_argument('--lobby-port', type=int, default=20200,
                    help='the lobby port to show when lobbyd has not '
                         'published one (default 20200, the port on the disc)')
    ap.add_argument('--trust-proxy', action='store_true',
                    help='take the client address from X-Forwarded-For.  Only '
                         'with a reverse proxy in front: the header is forged '
                         'trivially, and the login throttle depends on it')
    ap.add_argument('--reports-key', default='',
                    help='the secret in the abuse-reports page address, '
                         '/reports/<key>.  Default: made once and kept in '
                         'reports.key beside the database.  "off" disables '
                         'the page')
    ap.add_argument('--admin-key', default='',
                    help='the secret in the admin page address, /admin/<key>. '
                         'Default: made once and kept in admin.key beside the '
                         'database.  "off" disables the page')
    ap.add_argument('--news', default='',
                    help='the in-game news file the admin page edits; give '
                         'lobbyd the same one.  Default: news.txt beside the '
                         'database, which is also lobbyd\'s default')
    ap.add_argument('--logfile', default=DEFAULT_LOG,
                    help='the request log (default logs/webui.log beside '
                         'data/).  Empty for none')
    ap.add_argument('--log-max-mb', type=float, default=twlog.DEFAULT_MAX_MB,
                    help='roll the log over at this size (default %d MB)'
                         % twlog.DEFAULT_MAX_MB)
    ap.add_argument('--log-keep', type=int, default=twlog.DEFAULT_KEEP,
                    help='rolled-over copies to keep (default %d)'
                         % twlog.DEFAULT_KEEP)
    ap.add_argument('--quiet', action='store_true',
                    help='log requests to the file only, not the console too')
    args = ap.parse_args(argv)

    LOG = twlog.Log(args.logfile, max_bytes=args.log_max_mb * (1 << 20),
                    keep=args.log_keep, echo=not args.quiet)
    say = LOG.write         # startup lines go to the log as well

    BASE = '/' + args.base_path.strip('/') if args.base_path.strip('/') else ''
    TRUST_PROXY = args.trust_proxy
    SECURE_COOKIE = args.secure_cookie
    ADVERTISE = args.advertise
    LOBBY_PORT = args.lobby_port
    DB = twdb.DB(args.db)
    say('database %s -- %d accounts' % (DB.path, DB.count_accounts()))
    if args.reports_key != 'off':
        REPORTS_KEY = reports_key(DB.path, args.reports_key)
        say('abuse reports (%d open) -- private, do not share this address:'
              % DB.count_open_reports())
        say('    %s/reports/%s' % (BASE, REPORTS_KEY))
    NEWS_FILE = os.path.abspath(args.news or os.path.join(
        os.path.dirname(DB.path), 'news.txt'))
    if args.admin_key != 'off':
        ADMIN_KEY = reports_key(DB.path, args.admin_key, name='admin.key')
        say('admin page -- password resets, bans, renames, news; private:')
        say('    %s/admin/%s' % (BASE, ADMIN_KEY))
    say('lobbyd must be given this SAME path; it prints the one it opened.')
    say('sign-up site on http://%s:%d%s/'
          % ('localhost' if args.host in ('0.0.0.0', '') else args.host,
             args.port, BASE))
    if BASE and not TRUST_PROXY:
        say('!! mounted under %s but --trust-proxy is off, so every request'
              % BASE)
        say('   looks like it came from the proxy and the login throttle is')
        say('   one shared bucket shared by everybody')
    ip, lport = lobby_endpoint(ADVERTISE)
    if ip:
        say('the downloadable patch will point the game at %s:%d' % (ip, lport))
    else:
        say('!! cannot resolve this host\'s own address, so the patch')
        say('   download is disabled -- pass --advertise <host-or-ip>')
    say('(plain HTTP -- put it behind a reverse proxy with TLS before it faces '
          'the internet)')
    with Server((args.host, args.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            say('stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
