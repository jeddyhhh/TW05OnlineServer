"""The TW05 patch: what points Tiger Woods PGA Tour 2005 at a server.

Built here once, for the web site's downloads (webui.py) and for the
standalone TW05-MasterServerPatch.exe (patcher/tw05_patcher.py), so the two
can never disagree.

    python tw05patch.py 192.168.1.50      # print the .pnach for that address
"""
import sys

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
MAX_ADDR = min(size for _vaddr, size, _name in HOST_SLOTS) - 1   # 15

SERIAL, CRC = 'SLUS-21002', '88A808FA'
PNACH_NAME = '%s_%s.pnach' % (SERIAL, CRC)
LOBBY_PORT = 20200            # on the disc beside the name; not patchable
PNACH = """gametitle=Tiger Woods PGA Tour 2005 (USA) [%(serial)s]
comment=%(comment)s

// --- DNAS: treat the finished DNAS run as a pass ---
// 0x001BDDF4 loads the DNAS result (gDNASOutputBlock.iResult); zero is success.
patch=1,EE,001BDDF4,word,00002021
%(voice)s
// --- The master server: EA's host names replaced with %(ip)s ---
%(hosts)s"""
SITE_COMMENT = ("Online revival: master server -> %s. Downloaded from this "
                "server's web site.")

DNAS_PATCH = (0x001BDDF4, 0x00002021)

# Voice chat off.  At match load 0x001AEBE0 skips Voip_Init and Voip_Connect
# unless a flag is set (`beqz v0, 0x001AEBF8`); making that branch
# unconditional skips them always.  That is the state the game's own "not
# broadband" exit leaves (Voip_Init returns before touching anything, and
# Voip_Connect does nothing unless Voip_Init set 0x80000), so a match simply
# has no voice: no headset drivers loaded, and nothing on UDP 6000 -- the
# second fixed port.  Tried for two consoles behind one router playing
# through the online server (2026-10-01): voice was off, the match still
# dropped -- the relay (twrelay) is the fix for that; this stays as an option.
VOICE_OFF_PATCH = (0x001AEBE0, 0x10000005)       # beqz v0 -> b (always)
VOICE_OFF_PNACH = """
// --- Voice chat off: never start it, so nothing uses UDP 6000 ---
// 0x001AEBE0 skips Voip_Init/Voip_Connect when a flag is clear; now always.
patch=1,EE,001AEBE0,word,10000005
"""


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


def build_pnach(ip, voice=True, comment=None):
    """The whole .pnach for a server at `ip` (a dotted quad)."""
    return PNACH % {'serial': SERIAL, 'ip': ip,
                    'comment': comment or SITE_COMMENT % ip,
                    'voice': '' if voice else VOICE_OFF_PNACH,
                    'hosts': host_patches(ip)}


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


def cheat_codes(ip, voice=True):
    """(master lines, online lines): a name, then its codes."""
    master = ['Master Code', '9%07X %08X' % MASTER_CODE]
    online = ['TW05 Online - UNTESTED (server %s)' % ip,
              '2%07X %08X' % DNAS_PATCH]
    online += ['2%07X %08X' % w for _name, words in host_writes(ip)
               for w in words]
    if not voice:
        online.append('2%07X %08X' % VOICE_OFF_PATCH)
    return master, online


def build_cht(ip, voice=True):
    """Open PS2 Loader's <game ID>.cht (PS2rd format).  Every line that is
    not 16 hex digits is read as a cheat NAME, so it carries no comments."""
    master, online = cheat_codes(ip, voice)
    return '\n'.join(master + [''] + online) + '\n'


def build_cheatdevice(ip, voice=True):
    """Cheat Device's TXT database: the game title in quotes, then cheats."""
    master, online = cheat_codes(ip, voice)
    return '\n'.join(['"%s"' % REAL_PS2_TITLE] + master + [''] + online) + '\n'


if __name__ == '__main__':
    sys.stdout.write(build_pnach(sys.argv[1] if len(sys.argv) > 1
                                 else '127.0.0.1'))
