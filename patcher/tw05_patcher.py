"""Point Tiger Woods PGA Tour 2005 (PS2) at a master server.

TW05 looks its servers up by name (ps2tw05.ea.com and friends), and EA's are
long gone.  This writes the PCSX2 `.pnach` that replaces those names with a
server's address in memory at boot, and passes DNAS, then drops it into the
emulator's cheats folder with cheats switched on for this game.

    TW05-MasterServerPatch.exe                     interactive
    TW05-MasterServerPatch.exe --ip jeddyh.fyi --pcsx2 "C:\\PCSX2" --yes
    TW05-MasterServerPatch.exe --real-ps2          also write the (UNTESTED)
                                                   codes for a real PS2
    TW05-MasterServerPatch.exe --no-voice          switch voice chat off

Nothing is written to the ISO.  Deleting the .pnach puts the game back as it
was.  The patch is the same one the server's web site hands out (tw05patch).

ADDRESSES

The game takes the address as a dotted quad in place of each name, with room
for 15 characters, so a name typed here is resolved NOW and the IP written --
re-run this if the server's address changes.  The port is not asked: TW05's
is 20200, on the disc.

PCSX2's per-game settings file is updated, not replaced: only its
`EnableCheats` line is touched, so other settings and chosen cheats stay.
"""
import argparse
import ipaddress
import json
import os
import socket
import sys

if __package__ in (None, ''):
    # tw05patch sits in ../server here, and in .. in the GitHub layout.
    _up = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    sys.path[:0] = [os.path.join(_up, 'server'), _up]
import tw05patch

REMEMBER = 'tw05-masterserver.json'
TOOL = 'TW05-MasterServerPatch'


def here():
    """The directory to keep settings in -- beside the .exe once frozen."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def load_remembered():
    try:
        with open(os.path.join(here(), REMEMBER), encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                  # noqa: BLE001 - optional
        return {}


def remember(**values):
    try:
        with open(os.path.join(here(), REMEMBER), 'w', encoding='utf-8') as f:
            json.dump(values, f, indent=2)
            f.write('\n')
    except OSError as exc:
        print('  (could not save your answers: %s)' % exc)


def resolve(address):
    """(ip, note) for whatever the user typed, or raise ValueError."""
    address = address.strip()
    for prefix in ('http://', 'https://'):
        if address.lower().startswith(prefix):
            address = address[len(prefix):]
    address = address.split('/')[0]
    if address.count(':') == 1:
        address = address.split(':')[0]             # a port typed out of habit
    if not address:
        raise ValueError('no address given')
    try:
        return str(ipaddress.IPv4Address(address)), ''
    except ipaddress.AddressValueError:
        pass
    if ':' in address or address.replace('.', '').isdigit():
        raise ValueError('%r is not a valid IPv4 address' % address)
    try:
        ip = socket.gethostbyname(address)
    except OSError as exc:
        raise ValueError('could not resolve %r (%s)' % (address, exc))
    return ip, 'resolved %s -> %s' % (address, ip)


def looks_like_pcsx2(path):
    if not path or not os.path.isdir(path):
        return False
    return any(os.path.isfile(os.path.join(path, exe))
               for exe in ('pcsx2-qt.exe', 'pcsx2.exe', 'pcsx2-qtx64.exe'))


def find_installs(extra=()):
    """Every PCSX2 folder worth offering, most likely first, de-duplicated."""
    roots = [p for p in extra if p]
    base = here()
    roots += [base, os.path.dirname(base)]
    for root in (base, os.path.dirname(base), os.path.dirname(os.path.dirname(base))):
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        roots += [os.path.join(root, name) for name in entries
                  if 'pcsx2' in name.lower()]
    found, seen = [], set()
    for path in roots:
        full = os.path.abspath(path)
        key = full.lower()
        if key in seen:
            continue
        seen.add(key)
        if looks_like_pcsx2(full):
            found.append(full)
    return found


def ask(prompt, default=None):
    suffix = ' [%s]' % default if default else ''
    try:
        answer = input('%s%s: ' % (prompt, suffix)).strip()
    except EOFError:
        answer = ''
    return answer or (default or '')


def yes(prompt, default=False):
    answer = ask(prompt + (' (Y/n)' if default else ' (y/N)'),
                 'y' if default else 'n')
    return answer.lower().startswith('y')


def choose_install(remembered):
    found = find_installs([remembered])
    if found:
        print('\nPCSX2 installs found:')
        for n, path in enumerate(found, 1):
            print('  %d. %s' % (n, path))
        print('  0. somewhere else (type the path)')
        pick = ask('Which one', '1')
        if pick.isdigit() and 1 <= int(pick) <= len(found):
            return found[int(pick) - 1]
        if pick != '0' and looks_like_pcsx2(pick.strip('"')):
            return os.path.abspath(pick.strip('"'))
    else:
        print('\nNo PCSX2 install found automatically.')
    while True:
        path = ask('Path to the PCSX2 folder (the one with pcsx2-qt.exe), '
                   'or Enter to save the file here instead')
        if not path:
            return None
        path = os.path.abspath(path.strip('"'))
        if looks_like_pcsx2(path):
            return path
        print('  no pcsx2-qt.exe in %s' % path)
        if yes('Use it anyway?'):
            return path


def enable_cheats(ini_text):
    """`ini_text` with EnableCheats = true under [EmuCore], everything else
    exactly as it was."""
    lines = ini_text.splitlines()
    section, emucore_at, out, done = None, None, [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            if section == 'EmuCore' and not done:
                # [EmuCore] had no EnableCheats: add it before its blank tail
                while out and not out[-1].strip():
                    out.pop()
                out += ['EnableCheats = true', '', '']
                done = True
            section = stripped[1:-1]
            if section == 'EmuCore':
                emucore_at = len(out)
        elif section == 'EmuCore' and stripped.split('=')[0].strip() == 'EnableCheats':
            out.append('EnableCheats = true')
            done = True
            continue
        out.append(line)
    if not done:
        if section == 'EmuCore':
            out.append('EnableCheats = true')
        elif emucore_at is None:
            out = ['[EmuCore]', 'EnableCheats = true', ''] + out
    return '\n'.join(out).rstrip('\n') + '\n'


def install(pcsx2, text):
    """Write the patch (keeping any different one as .bak) and switch cheats on
    for this game.  Returns (pnach path, ini path, backup path or None)."""
    cheats = os.path.join(pcsx2, 'cheats')
    os.makedirs(cheats, exist_ok=True)
    path = os.path.join(cheats, tw05patch.PNACH_NAME)
    backup = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            old = f.read()
    except OSError:
        old = None
    if old is not None and old != text:
        backup = path + '.bak'
        with open(backup, 'w', encoding='utf-8', newline='\n') as f:
            f.write(old)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)

    gamesettings = os.path.join(pcsx2, 'gamesettings')
    os.makedirs(gamesettings, exist_ok=True)
    ini = os.path.join(gamesettings, '%s_%s.ini' % (tw05patch.SERIAL,
                                                    tw05patch.CRC))
    try:
        with open(ini, encoding='utf-8') as f:
            current = f.read()
    except OSError:
        current = ''
    with open(ini, 'w', encoding='utf-8', newline='\n') as f:
        f.write(enable_cheats(current))
    return path, ini, backup


def main():
    ap = argparse.ArgumentParser(
        description='Point TW05 at a master server.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ip', help='master server address (IPv4, or a name to '
                                 'resolve, e.g. jeddyh.fyi)')
    ap.add_argument('--pcsx2', help='PCSX2 folder to install into')
    ap.add_argument('--no-voice', action='store_true',
                    help='switch voice chat off in matches (nothing on UDP 6000)')
    ap.add_argument('--real-ps2', action='store_true',
                    help='also write %s (Open PS2 Loader) and %s (Cheat '
                         'Device) beside this program -- the same patch for a '
                         'real console.  UNTESTED on real hardware'
                         % (tw05patch.CHT_NAME, tw05patch.CHEATDEVICE_NAME))
    ap.add_argument('--yes', action='store_true', help='take the defaults, ask nothing')
    args = ap.parse_args()

    saved = load_remembered()
    interactive = not args.yes

    print('Tiger Woods PGA Tour 2005 -- master server patch')
    print('=' * 48)

    # --- the address -------------------------------------------------------
    default_ip = args.ip or saved.get('address') or ''
    while True:
        raw = args.ip if (args.ip and not interactive) else (
            ask('\nMaster Server address (IP or name)', default_ip)
            if interactive else default_ip)
        try:
            ip, note = resolve(raw)
        except ValueError as exc:
            print('  %s' % exc)
            if not interactive:
                return 2
            args.ip = None
            continue
        if len(ip) > tw05patch.MAX_ADDR:
            print('  %s is %d characters; the game only has room for %d'
                  % (ip, len(ip), tw05patch.MAX_ADDR))
            if not interactive:
                return 2
            args.ip = None
            continue
        if note:
            print('  %s' % note)
        break

    # --- voice -------------------------------------------------------------
    voice = not args.no_voice
    if interactive and not args.no_voice:
        voice = not yes('Switch voice chat off? Only if the server asks you to',
                        default=bool(saved.get('voice_off')))

    # --- where to put it ---------------------------------------------------
    pcsx2 = args.pcsx2 or (saved.get('pcsx2') if args.yes else None)
    if interactive and not args.pcsx2:
        pcsx2 = choose_install(saved.get('pcsx2'))
    if pcsx2:
        pcsx2 = os.path.abspath(pcsx2)

    text = tw05patch.build_pnach(
        ip, voice, comment='Master server -> %s%s. Generated by %s.'
                           % (ip, '' if voice else ', voice chat off', TOOL))

    if pcsx2:
        path, ini, backup = install(pcsx2, text)
        print('\nWritten:')
        print('  %s' % path)
        if backup:
            print('  (the patch that was there is kept as %s)'
                  % os.path.basename(backup))
        print('  %s  (EnableCheats on; nothing else changed)' % ini)
    else:
        path = os.path.join(here(), tw05patch.PNACH_NAME)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        print('\nWritten:\n  %s' % path)
        print('\nNo PCSX2 folder chosen, so copy that file into <PCSX2>/cheats/')
        print('and turn on Enable Cheats for the game yourself.')

    if args.real_ps2:
        print('\nFor a real PS2 (UNTESTED -- nobody has tried these on a console):')
        for name, body in ((tw05patch.CHT_NAME, tw05patch.build_cht(ip, voice)),
                           (tw05patch.CHEATDEVICE_NAME,
                            tw05patch.build_cheatdevice(ip, voice))):
            extra = os.path.join(here(), name)
            with open(extra, 'w', encoding='utf-8', newline='\n') as f:
                f.write(body)
            print('  %s' % extra)

    # Keep the last install that actually worked: a run that ended up writing
    # beside the tool must not erase it, or the next run has nothing to offer.
    remember(address=raw.strip() or ip, voice_off=not voice,
             pcsx2=pcsx2 or saved.get('pcsx2', ''))

    print('\nTW05 will now use the server at %s (port %d)'
          % (ip, tw05patch.LOBBY_PORT))
    if raw.strip() != ip:
        print('That is %r as it resolves today -- re-run this if it changes.'
              % raw.strip())
    print('\nNext, in PCSX2:')
    print('  1. Settings > Network & HDD: tick Enabled, set the device type to')
    print('     Sockets and pick the network adapter you use (on Linux or a')
    print('     Steam Deck leave it on Auto).  No host names or DNS to set:')
    print('     the patch writes the address in.')
    print('  2. Restart the game -- the patch is applied at boot, so a game')
    print('     already running is still using the old address.')
    print('  3. Choose PLAY ONLINE.  The first time, the game may offer to make')
    print('     a network configuration on the memory card: accept and save the')
    print('     defaults.')
    print('  4. Sign in with an account made on the server\'s web site.')

    if interactive:
        ask('\nPress Enter to close')
    return 0


if __name__ == '__main__':
    sys.exit(main())
