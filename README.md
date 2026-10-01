# Tiger Woods PGA Tour 2005 — online master server

A replacement for EA's long-dead online service for **Tiger Woods PGA Tour 2005**
on the PlayStation 2, for playing online through the
[PCSX2](https://pcsx2.net) emulator.

My server is online at [jeddyh.fyi/TW05Online](https://jeddyh.fyi/TW05Online).

Create an account on the website and use it to log into the game's online
mode; the in-game account creation does not work.

It brings back the game's online menus:

- **Lobby:** accounts and personas, game rooms and chat.
- **Head-to-head play** in all four of TW05's modes: stroke play, match play,
  the **3 Hole Mini-Game**, and **Battle** (match play where winning a hole
  lets you take a club out of your opponent's bag). Games are advertised in
  the room and joined from the list, and results are recorded.
- **Online tournaments:** a daily event on the game's calendar, with its own
  leaderboards and prize money.
- **Online cash:** every golfer starts with the same balance, tournament
  prize money adds to it, and head-to-head games can be played for a wager.
- **MY RESUME:** your records and statistics on the in-game screen.
- **EA Messenger:** player search, friend requests, buddy lists, who's online, and messages between players.
- **In-game news:** your own text, plus a digest the server writes from
  recent results.
- **Feedback:** the game's Feedback screen. Compliments (good attitude, great
  session) raise a player's REP on MY RESUME and show on their web page;
  complaints lower it and go to the operator's private reports page with the
  chat that led up to them.

Alongside the lobby runs a **web site**, styled after the game's own menus.
Players create their account and download the game patch there. It also shows
live server status, leaderboards and the cash list, the tournament calendar
with a page for every event, monthly seasons and a Hall of Fame, player
profiles with handicaps and achievements, head-to-head records, side-by-side
comparisons, tour stats with a 30-day activity chart, records, and course
pages. A private admin page covers password resets, bans, renames and the
in-game news.

Both are plain Python with **no dependencies**: no framework, no database
server, no build step.

This grew out of the [TW04 server](https://github.com/jeddyhhh/TW04OnlineServer):
the two games share EA's lobby protocol, and TW05's differences -- how it finds
the server, its tournament calendar, game adverts, statistics, cash and voice
chat -- are written up in [docs/](docs/README.md). The scripts used to read the
game's executable are in [analysis/](analysis/README.md).

> This is a fan project. It is not affiliated with, endorsed by or connected to
> Electronic Arts or Sony. No game files, BIOS or disc images are included, and
> none are needed to run the server.

---

## Contents

- [What you need](#what-you-need)
- [Quick start](#quick-start)
- [How it fits together](#how-it-fits-together)
- [Running it for real](#running-it-for-real)
- [How players connect](#how-players-connect)
- [Looking after it](#looking-after-it)
- [Options](#options)
- [Files it creates](#files-it-creates)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)
- [License](#license)

---

## What you need

- **Python 3.8 or newer.** There's nothing to `pip install`. The SQLite built
  into Python must be 3.24 or newer, which any Python from the last several
  years has (`python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`).
- A machine that players can reach on:

| Port | Protocol | What uses it |
|---|---|---|
| 20200 | TCP | the lobby (`lobbyd.py`); consoles connect straight to it |
| 13505 | TCP | EA Messenger (buddy lists, presence, messages), inside `lobbyd.py` |
| 8081 | TCP | the web site (`webui.py`), or whatever you put in front of it |

The lobby and Messenger ports are fixed: they're on the game disc beside the
server names, and nothing changes them.

The game itself (a match between two players) is peer-to-peer over **UDP 3658**
directly between the players. It never goes through this server; see
[Known limits](#known-limits).

It can run on the same machine as the TW04 server: none of the ports overlap
(TW04 uses 10200, 10202 and 8080), and each keeps its own folder and database.

## Quick start

```bash
git clone https://github.com/jeddyhhh/TW05OnlineServer.git
cd TW05OnlineServer

python3 webui.py --no-secure-cookie     # the web site, on port 8081
python3 lobbyd.py                       # the lobby, on port 20200
```

Run each in its own terminal. Then open `http://localhost:8081/`, create an
account, and follow the **Connect from PCSX2** steps on that page to point a
game at the server. `--no-secure-cookie` lets you sign in over plain HTTP from
another machine on your network; leave it off once the site is behind HTTPS.

Both programs open the same database, `data/tw05.db`, next to the scripts. It's
created the first time either one starts. Each prints the full path it opened.
**If those two paths ever differ, accounts made on the web site won't exist at
the game's login screen.**

## How it fits together

```
 PCSX2 + the game ──TCP 20200──▶  lobbyd.py  ──┐
          │         ──TCP 13505──▶  (Messenger) │
          │                                    ├──▶  data/tw05.db  (SQLite)
          │                                    │
          └── UDP 3658 to the other player     │
                                               │
 web browser ─────────HTTP──────▶  webui.py  ──┘
```

- **`lobbyd.py`** talks to the consoles. It speaks EA's lobby protocol, lists
  the games players advertise, introduces the two players when one joins, and
  records results, tournament rounds and cash. It also writes the live picture
  (who's online, matches in play) into the database for the web site.
- **`webui.py`** is the web site. It only ever reads what the lobby has
  written, apart from account sign-up and account management.
- **The game patch.** TW05 finds EA's servers by name (`ps2tw05.ea.com` and
  two others), and the patch writes your server's IP address over those names
  in the game, along with a fix for the dead DNAS check. The web site builds
  it (a PCSX2 `.pnach` file) for *your* server on demand, so there's nothing to
  prepare by hand, and players don't need to change any DNS settings.

The supporting modules, for anyone reading the code:

| File | What it is |
|---|---|
| `twdb.py` | the database: accounts, personas, results, tournaments, cash, buddy lists, reports |
| `twrecords.py` | player pages, stat leaders, records, course pages, cash, and the news digest |
| `twtourney.py` | the online tournament calendar and its wire format |
| `twstats05.py` | the packed statistics record behind MY RESUME |
| `twstats.py` | the course list |
| `tagfield.py`, `eacrypt.py` | EA's message encoding and the login password cipher |
| `twlog.py` | the size-capped log both programs write |
| `twrelay.py` | the UDP relay for matches whose consoles can't reach each other directly |
| `tw05patch.py` | the game patch (host names, DNAS, real-PS2 codes), shared by the site and the patcher |
| `patcher/tw05_patcher.py` | `TW05-MasterServerPatch.exe`: writes the patch into a PCSX2 install for any server |
| `tw05.sh` | runs and supervises both on Linux (see below) |
| `docs/` | the research notes: how everything above was worked out |
| `analysis/` | the ELF analysis helpers those notes were worked out with (they need `capstone` and `pyelftools`; see [analysis/README.md](analysis/README.md)) |

## Running it for real

### As a service (Linux)

`tw05.sh` starts both programs and restarts either one a few seconds after it
exits, so a crash doesn't need anyone to notice:

```bash
chmod +x tw05.sh
ADVERTISE=<your public IPv4> ./tw05.sh start   # start both; safe to run again
./tw05.sh start webui                          # just one of them
./tw05.sh status
./tw05.sh restart [lobbyd|webui]
./tw05.sh stop [lobbyd|webui]
./tw05.sh log lobbyd                           # follow a log
```

Settings are at the top of the script, or can be given in the environment:

| Setting | Default | Meaning |
|---|---|---|
| `ADVERTISE` | *(empty)* | the address written into every patch players download; **set it** (see below) |
| `START_CASH` | `10000` | the online cash every golfer starts with |
| `RELAY` | `off` | the match relay on UDP 3658, experimental: `same` for two consoles behind one router that can't be given LAN addresses, `all` for every match, `off` never |
| `RELAY_ADDR` | *(empty)* | the address to give consoles for the relay, if this machine is behind NAT |
| `VOICE` | `on` | `off` puts a patch in the downloads that never starts voice chat, so matches use only UDP 3658 |
| `WEB_HOST` | `127.0.0.1` | where the web site listens; keep it local behind a reverse proxy |
| `WEB_PORT` | `8081` | the web site's port |
| `WEB_BASE` | `/TW05Online` | the path the site is served under |
| `SECURE_COOKIE` | `1` | `0` lets sign-in work over plain HTTP, for a home network only |
| `BUDDY_PORT` | `13505` | EA Messenger's TCP port; empty to run without it |
| `BUDDY_ADDR` | *(empty)* | `host:port` to give consoles for Messenger, if this machine's own address isn't the public one (behind NAT) |
| `DB` / `NEWS` | `data/tw05.db` / `data/news.txt` | the database and the news text both programs share |
| `LOG_MAX_MB` / `LOG_KEEP` | `10` / `3` | each log rolls over at this size, keeping this many old copies |
| `PYTHON` | `python3` | the interpreter to use |

**`ADVERTISE` matters.** It's the address the site writes into the patch. Left
empty, the site looks up the hostname it was reached by, and on a server that
often resolves to a local address such as `127.0.1.1`, so every patch handed
out would point there. Put it at the top of `tw05.sh` so cron starts the site
the same way. If the server's address ever changes, players download the patch
again.

To start everything at boot, add this to `crontab -e`, as the user that owns
the files rather than root:

```cron
@reboot /path/to/repository/tw05.sh start
```

Optionally, add a watchdog that restarts anything that isn't running:

```cron
* * * * * /path/to/repository/tw05.sh start >/dev/null 2>&1
```

### Behind a web server, with HTTPS

The web site is plain HTTP, so put it behind a reverse proxy that handles TLS.
With Apache, serving it at `https://example.com/TW05Online`:

```apache
# a2enmod proxy proxy_http
ProxyPreserveHost On
ProxyPass        /TW05Online  http://127.0.0.1:8081/TW05Online
ProxyPassReverse /TW05Online  http://127.0.0.1:8081/TW05Online
```

and run the site on localhost with the matching path, which `tw05.sh` does by
default. Next to a TW04 server, add these two `ProxyPass` lines under its
`/TW04Online` ones, in the same `<VirtualHost>`.

- **`--trust-proxy` matters here** (`tw05.sh` passes it). Without it, every
  visitor appears to come from `127.0.0.1`, so the site's lockout after
  repeated wrong passwords becomes one shared counter for everybody. Only use
  it with a proxy actually in front, because anyone who can reach the port
  directly can fake the header it reads.
- **The session cookie is marked `Secure`.** `--no-secure-cookie`
  (`SECURE_COOKIE=0`) exists only for plain HTTP on a home network.

Only the web site goes behind the proxy. Consoles connect straight to TCP 20200
and 13505, so open both on your firewall (and UDP 3658 too if you turn on the
experimental match relay, `RELAY`); on Ubuntu:

```bash
sudo ufw allow 20200/tcp
sudo ufw allow 13505/tcp
```

### On a home network

With no proxy and no HTTPS, let other machines reach the site and let the
sign-in cookie travel over plain HTTP:

```bash
WEB_HOST=0.0.0.0 SECURE_COOKIE=0 ADVERTISE=192.168.1.x ./tw05.sh start
```

and browse to `http://192.168.1.x:8081/TW05Online/`.

### Before it faces the internet

- Put HTTPS in front of the web site (above).
- Set `ADVERTISE` to the server's public address.
- Don't run `lobbyd.py` with `--open`. It creates an account for anyone who
  types a new name at the console, which is handy on a LAN and wrong on the
  internet.
- Open TCP 20200 and 13505 on the firewall.

## How players connect

The web site's front page walks players through all of this and gives them the
patch file. In short, in PCSX2:

1. **Install the patch.** Download the `.pnach` file from the site and put it
   in PCSX2's `cheats` folder, keeping its name exactly: PCSX2 matches it to
   the disc by name. Then right-click the game → *Properties* → *Patches* and
   tick **Enable Cheats**.
2. **Turn on networking.** Settings → Network & HDD:
   - tick **Enable Network (DEV9)**;
   - set the Ethernet device type to **Sockets**;
   - set **Ethernet Device** to the network adapter you use on **Windows**, and
     to **Auto** on **Linux and the Steam Deck**.

   Leave the DNS settings and the host list alone: the patch carries the
   server's address.
3. **Save a network configuration (first time only).** The game needs a
   PlayStation 2 network configuration on the memory card. If there isn't one,
   it offers to create one; just save the default settings.
4. **Sign in.** Create an account on the web site, then choose PLAY ONLINE on
   the console, and on SELECT EA ACCOUNT pick USE EXISTING EA ACCOUNT and type
   the same account name and password.

**Or let the patcher do step 1.** `patcher/tw05_patcher.py` (built as
`TW05-MasterServerPatch.exe`) asks for the server's address, finds the PCSX2
installs beside it, writes the same patch into `cheats`, and turns on Enable
Cheats for TW05 without touching the game's other settings. A patch that was
already there is kept as `.pnach.bak`. Build it with:

```bash
python -m PyInstaller --onefile --console --name TW05-MasterServerPatch --paths . --hidden-import tw05patch patcher/tw05_patcher.py
```

The patch never modifies the disc image. Deleting the `.pnach` file undoes it.

It works with **Tiger Woods PGA Tour 2005, USA (NTSC-U), serial `SLUS-21002`,
CRC `88A808FA`**, and no other release.

### On a real PS2 (untested)

The front page also offers the same patch as cheat codes for a real console's
cheat engine: `SLUS_210.02.cht` for Open PS2 Loader, and a Cheat Device list.
They need a PS2 that can run homebrew, with a network adapter. A game-specific
master code hooks the game; TW05's published CodeBreaker one is encrypted, so
this one was found in the executable instead (a call made every frame from the
controller-reading code, confirmed in PCSX2's debugger; see
[docs/tw05-recon.md](docs/tw05-recon.md)). **Nobody has tried them on a real
console yet.** If you do, whether they work or not, please
[open an issue](https://github.com/jeddyhhh/TW05OnlineServer/issues) with what
happened.

## Looking after it

### The admin page

Most day-to-day jobs are on a page that nothing links to:

```
<your site>/admin/<key>
```

Find an account by its name or any of its personas, then:

- **Reset its password.** Type one, or leave the box empty and the page makes
  up an 8-character one that's easy to type on the console keyboard. The new
  password is shown once, on the page. Players can't recover a password by
  themselves, so this is how a forgotten one gets fixed.
- **Ban the account, or lift a ban.** A ban blocks signing in, on the console
  and on the web site, for all of the account's personas. Anyone already in
  the lobby stays until they leave.
- **Rename a persona.** Its results, tournament rounds, buddies and reports
  move with it. The page refuses while that persona is online.
- **Delete the account.** It removes the account, its personas, and every
  round and match they played, for good. Type the account name to confirm.
  Abuse reports about them are kept as the moderation record.

The same page edits the in-game news (see below).

The key is created the first time the web site starts, kept in
`data/admin.key`, and printed at every start. Anyone with the address can do
all of the above, so keep it private. To change it, delete the file and restart
the site. `webui.py --admin-key off` turns the page off.

### Accounts from the shell

```bash
python3 twdb.py --list
python3 twdb.py --create-account NAME --password PASS [--persona PERSONA]
python3 twdb.py --add-persona ACCOUNT PERSONA
python3 twdb.py --set-password ACCOUNT PASS
python3 twdb.py --disable ACCOUNT        # a ban; --enable undoes it
```

Players normally manage their own accounts and personas on the web site. An
account can hold up to four personas.

### Online cash

Every golfer starts with `--start-cash` (default $10,000). Their prize money
from finished tournament days adds to it, and head-to-head games can be played
for a wager, from $1,000 up to $1,000,000: the game takes both players' stakes
when the match starts, the winner collects the pot, and a tie hands each stake
back. The balance shows as ONLINE EARNINGS on MY RESUME, and on the web site's
leaderboard and each player's page, where signed-in players also see where
their cash came from.

### News

The game's news screen shows the text of `data/news.txt`, if it exists. Edit
it on the admin page, or by hand. It's re-read on every request, so a change
shows up for the next player who opens the screen. The PS2 can only show plain
ASCII, and the admin page won't save anything else. Below it, the server adds
a digest it writes itself: today's event and leader, yesterday's winner, the
week's new records, and the busiest player. `--no-auto-news` turns the digest
off.

### Backups

The lobby copies the database once a day into `data/backups/`, as
`tw05-YYYY-MM-DD.db`, and keeps the newest 7. It uses SQLite's own backup, so
the copy is consistent even while players are on. `--backup-keep N` changes
how many are kept (`0` turns backups off), and `--backup-dir` puts them
somewhere else, such as another disk.

To restore one, stop both programs, copy the backup over `data/tw05.db`,
delete `data/tw05.db-wal` and `data/tw05.db-shm` if they exist, and start them
again.

### Abuse reports

When a player uses REPORT ABUSE in the game, the report is stored with the
chat the server relayed in the hour before. Reports are listed on a page that
nothing links to:

```
<your site>/reports/<key>
```

The key is created the first time the web site starts, kept in
`data/reports.key`, and printed at every start. Anyone with the address can
read the reports, so keep it private. To change it, delete the file and
restart the site. `webui.py --reports-key off` turns the page off. To ban
someone, use the admin page or `twdb.py --disable`.

## Options

The most useful ones; `--help` on either program lists them all.

**`lobbyd.py`**

| Option | Default | Meaning |
|---|---|---|
| `--port N` | `20200` | the lobby's TCP port; the game only ever dials 20200 |
| `--host ADDR` | `0.0.0.0` | where to listen; `::` takes IPv6 and IPv4 together |
| `--buddy-port N` | `13505` | EA Messenger's port; `0` turns it off |
| `--buddy-addr HOST:PORT` | | the Messenger address to give consoles, when this machine's own address isn't the public one |
| `--start-cash N` | `10000` | the online cash every golfer starts with |
| `--db PATH` | `data/tw05.db` | the database; must be the same file the web site uses |
| `--news FILE` | `data/news.txt` | the news screen text |
| `--no-auto-news` | | the news file only, without the generated digest |
| `--backup-keep N` | `7` | daily database copies to keep; `0` turns backups off |
| `--backup-dir DIR` | `data/backups` | where the daily copies go |
| `--open` | | create an account on first login instead of refusing it (LAN only) |
| `--logfile FILE` | `logs/lobbyd.log` | the log; `''` for none |
| `--quiet` | | log to the file only, not the terminal too |
| `--log-max-mb` / `--log-keep` | `10` / `3` | log rollover size and copies kept |
| `-v, --verbose` | | hex dump of every message, for debugging |

**`webui.py`**

| Option | Default | Meaning |
|---|---|---|
| `--port N` | `8081` | the site's port |
| `--host ADDR` | `0.0.0.0` | where to listen; use `127.0.0.1` behind a proxy |
| `--base-path PATH` | | serve under a path, e.g. `/TW05Online` |
| `--trust-proxy` | | read the visitor's address from `X-Forwarded-For` (proxy only) |
| `--advertise HOST` | | the address the downloadable patch points players at |
| `--no-secure-cookie` | | for plain HTTP only |
| `--reports-key KEY` | *(from `data/reports.key`)* | the abuse-reports page's secret; `off` turns the page off |
| `--admin-key KEY` | *(from `data/admin.key`)* | the admin page's secret; `off` turns the page off |
| `--news FILE` | `data/news.txt` | the news file the admin page edits; must be the lobby's `--news` |
| `--db PATH` | `data/tw05.db` | the database; must be the same file the lobby uses |
| `--logfile FILE` | `logs/webui.log` | the request log; `''` for none |

## Files it creates

Everything the servers write goes in two folders next to the scripts. Both are
in `.gitignore`.

| Path | What it is |
|---|---|
| `data/tw05.db` | the database: every account and password hash, results, tournaments, cash, buddy lists and reports |
| `data/backups/` | a copy of the database for each of the last 7 days (see [Backups](#backups)) |
| `data/reports.key` | the secret in the abuse-reports page's address |
| `data/admin.key` | the secret in the admin page's address |
| `data/news.txt` | your news text, if you create it |
| `logs/lobbyd.log`, `logs/webui.log` | the logs, each capped at `LOG_MAX_MB` with `LOG_KEEP` old copies |

Passwords are stored as salted PBKDF2 hashes, and they're kept out of the logs
unless you start the lobby with `--log-passwords`, a debugging switch that
says so loudly when it's on.

## Tests

Each test starts a real server on a spare port with a throwaway database, and
drives it the way the game does:

```bash
python3 tests/tourney_test.py    # date, season, calendar, a round played and reported
python3 tests/games_test.py      # game adverts, joining, all four modes, wagers and cash
python3 tests/messenger_test.py  # EA Messenger: search, friend requests, buddies, messages
python3 tests/address_test.py    # which address each console is given for peer to peer
python3 tests/webui_test.py      # every page, the patch and cheat files, cash on the site
```

Most modules also test themselves: `python3 twrecords.py`, `twstats05.py`,
`twtourney.py`, `twrelay.py`, `twstats.py`, `twlog.py`, `tagfield.py` and
`eacrypt.py`.

## Troubleshooting

- **Nobody can sign in, but the web site works.** The two programs are using
  different databases. Compare the path each one prints at startup.
- **The game stops at the DNAS screen.** The patch isn't active: Enable Cheats
  is off, the file was renamed, or the game was started before the file was in
  place.
- **It passes DNAS, then can't find the server.** Check the lobby is running
  and TCP 20200 is open, and that the patch points at the right address: its
  first lines say where. A patch from before the server moved points at the
  old address, so download it again, and set `ADVERTISE` if the site handed
  out a wrong one. As a last resort, set PCSX2's DNS1 to **Internal**.
- **Players get into the lobby, but matches never start.** The two players have
  to reach each other on UDP 3658, as the lobby only introduces them. Check each
  player can receive on that port (a port forward, behind a home router). On
  Linux or a Steam Deck, check the Ethernet Device is set to **Auto**. Wi-Fi
  networks that isolate devices from each other also break this.
- **What happened to that connection?** `logs/lobbyd.log` records every
  message both ways, with passwords redacted (unless the lobby was started
  with `--log-passwords`). Add `-v` for a hex dump of each message too.

## Known limits

- **Matches are peer-to-peer.** The two consoles play each other directly over
  UDP 3658, and there's no relay. Each player needs that port reachable (a port
  forward behind a home router). Two players behind the same router can play
  each other through an internet-hosted lobby only if their router supports
  "hairpin" NAT (NAT loopback); many do.
- **IPv4 only, for players.** The game can only express IPv4 addresses. The
  lobby can listen on IPv6 (`--host ::`), but a player connecting over IPv6
  can't be introduced to an opponent.
- **Two emulators on one Windows PC can't play each other.** Windows won't pass
  UDP between two local network adapters; use two machines.
- **Some MY RESUME lines aren't right yet:** DID NOT FINISH and the last-10
  DNF count, and the rank lines, which come from a request (`usrrk`) the
  server doesn't answer yet. See [docs/tw05-stats.md](docs/tw05-stats.md).
- **Voice chat is untested.** The game supports a USB headset in matches, and
  the notes describe how ([docs/tw05-voice.md](docs/tw05-voice.md)), but nobody
  has heard it working through this server yet.
- **DNAS is passed, not removed.** The patch makes the game's dead Sony DNAS
  check succeed, but the game still tries to reach Sony first.
- **Only the one disc.** The patch is a list of addresses in one build of the
  game (`SLUS-21002`). Other regions aren't supported, and Tiger Woods PGA Tour
  2004 has [its own server](https://github.com/jeddyhhh/TW04OnlineServer).

## License

The server code is released under the [MIT License](LICENSE).

Tiger Woods PGA Tour 2005 is a trademark of Electronic Arts; PlayStation and
DNAS are trademarks of Sony Interactive Entertainment. This project contains no
code, data or assets from the game, and you need your own legally obtained copy
to play.
