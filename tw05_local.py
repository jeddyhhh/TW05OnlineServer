"""The TW05 lobby and web site together, for playing on a home network.

    TW05-LocalServer.exe                    double-click it
    python tw05_local.py                    the same, from source
    python tw05_local.py --ip 192.168.1.20 --web-port 8082

Runs `lobbyd` (with EA Messenger) and `webui` in one process, set up for a
LAN rather than the internet -- TW04-LocalServer's twin, on TW05's ports, so
the two can run side by side:

* everything it writes goes beside the exe -- `data\\` and `logs\\` -- because
  a one-file exe unpacks itself to a temporary folder that is deleted when it
  closes, and the database would go with it;
* the web site hands out a patch that points at THIS machine's LAN address.
  PCSX2 cannot use `localhost` for the game (its network adapter sources
  traffic from a real network card), so the address has to be the card's own;
* sign-in works over plain http:// from other machines (no Secure cookie --
  there is no HTTPS on a LAN);
* the console can sign in with any new name and password (`--open`), so
  friends on the network need not visit the web site first.

The game's ports are on the disc -- the lobby on 20200, Messenger on 13505 --
so only the web site's can be changed.  Closing the window stops everything.
"""
import argparse
import os
import socket
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lobbyd
import twdb
import webui

LOBBY_PORT = 20200            # TW05 dials these; they are not ours to move
BUDDY_PORT = 13505
WEB_PORT = 8081


def home():
    """Where data\\ and logs\\ go: beside the exe, or, run from source, where
    the database would normally be (TW05Online)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(twdb.DEFAULT_DB))


def lan_address():
    """This machine's address on the network it would use to reach the
    internet.  A UDP 'connect' sends nothing; it only asks the OS which card
    and which address it would use.  None when there is no network at all."""
    for probe in ('192.168.1.1', '10.0.0.1', '8.8.8.8'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
            if ip and not ip.startswith(('127.', '0.')):
                return ip
        except OSError:
            continue
    return None


def other_addresses(skip):
    try:
        found = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(),
                                                     None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(a for a in found
                  if a != skip and not a.startswith(('127.', '169.254.')))


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('0.0.0.0', port))
            return True
        except OSError:
            return False


def stop(message=None):
    """Leave the window open long enough to read why, then exit."""
    if message:
        print()
        print(message)
    print()
    try:
        input('Press Enter to close this window.')
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(1 if message else 0)


def run(name, target, argv, failed):
    try:
        target(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            failed.append('%s stopped (exit %s)' % (name, exc.code))
    except Exception as exc:                            # noqa: BLE001
        failed.append('%s stopped: %s' % (name, exc))


def main():
    ap = argparse.ArgumentParser(
        description='Tiger Woods PGA Tour 2005 online, for a home network: '
                    'the lobby, EA Messenger and the web site together.')
    ap.add_argument('--ip', help='the LAN address players should use for this '
                                 'machine (default: detected)')
    ap.add_argument('--web-port', type=int, default=WEB_PORT)
    ap.add_argument('--start-cash', type=int, default=10000,
                    help='the online cash every golfer starts with')
    ap.add_argument('--no-voice', action='store_true',
                    help='hand out the patch with voice chat switched off')
    ap.add_argument('--closed', action='store_true',
                    help='do not create accounts at the console; players must '
                         'sign up on the web site first')
    args = ap.parse_args()
    # Line at a time, so the window shows each line as it is printed even
    # when stdout is not a console (and nothing is lost if it is killed).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    base = home()
    data = os.path.join(base, 'data')
    logs = os.path.join(base, 'logs')
    db = os.path.join(data, 'tw05.db')
    os.makedirs(data, exist_ok=True)

    print('Tiger Woods PGA Tour 2005 -- local online server')
    print('=' * 50)

    ip = args.ip or lan_address()
    if not ip:
        stop('This computer does not seem to be on a network.  Connect it to '
             'your router\n(Wi-Fi or cable) and start this again, or give the '
             'address with --ip.')

    busy = [p for p in (LOBBY_PORT, BUDDY_PORT, args.web_port)
            if not port_free(p)]
    if busy:
        stop('Port%s %s already in use -- is this server (or another copy of '
             'it) already\nrunning?  Close it and try again.'
             % ('s' if len(busy) > 1 else '',
                ', '.join(str(p) for p in busy)))

    # Create the database once, here, before either half starts.  Left to
    # them, a first run has both building a brand-new file at the same moment
    # (TW04-LocalServer's first test run had the web site fail to come up).
    try:
        twdb.DB(db).conn.close()
    except Exception as exc:                            # noqa: BLE001
        stop('Could not create the database at %s:\n%s' % (db, exc))

    failed = []
    lobby_argv = ['--port', str(LOBBY_PORT),
                  '--buddy-port', str(BUDDY_PORT),
                  '--db', db,
                  '--news', os.path.join(data, 'news.txt'),
                  '--start-cash', str(args.start_cash),
                  '--logfile', os.path.join(logs, 'lobbyd.log'),
                  '--quiet']
    if not args.closed:
        lobby_argv.append('--open')
    web_argv = ['--port', str(args.web_port),
                '--db', db,
                '--news', os.path.join(data, 'news.txt'),
                '--advertise', ip,
                '--lobby-port', str(LOBBY_PORT),
                '--no-secure-cookie',
                '--logfile', os.path.join(logs, 'webui.log'),
                '--quiet']
    if args.no_voice:
        web_argv.append('--no-voice')
    for name, target, argv in (('the lobby', lobbyd.main, lobby_argv),
                               ('the web site', webui.main, web_argv)):
        threading.Thread(target=run, args=(name, target, argv, failed),
                         daemon=True, name=name).start()

    time.sleep(1.5)
    if failed:
        stop('Could not start: %s\nSee %s for details.'
             % ('; '.join(failed), logs))

    site = 'http://%s:%d/' % (ip, args.web_port)
    print()
    print('  Running.  Keep this window open while you play.')
    print()
    print('  Web site     %s' % site)
    print('               sign up, and download the game patch, from here')
    print('  Game server  %s  (lobby port %d, Messenger %d)'
          % (ip, LOBBY_PORT, BUDDY_PORT))
    print('               the patch points the game here')
    if webui.ADMIN_KEY:
        # Only this PC should open it: it resets passwords and bans players.
        print('  Admin page   http://localhost:%d/admin/%s'
              % (args.web_port, webui.ADMIN_KEY))
        print('               passwords, bans, renames, news -- keep it private')
    print()
    if args.closed:
        print('  Players must create an account on the web site first.')
    else:
        print('  Players can sign in at the console with any new name and')
        print('  password -- the account is created on first sign-in.')
    print()
    print('  On each PC with PCSX2:')
    print('    1. open the web site above and download the patch (or run')
    print('       TW05-MasterServerPatch.exe with %s);' % ip)
    print('    2. follow "Connect from PCSX2" on that page.')
    print()
    print('  If Windows asks whether to allow this program on the network,')
    print('  allow it on PRIVATE networks, or other PCs cannot connect.')
    others = other_addresses(ip)
    if others:
        print()
        print('  This PC has other addresses too: %s.' % ', '.join(others))
        print('  If the other PCs cannot open the web site above, make a')
        print('  shortcut to this program and add  --ip <the address on your')
        print('  home network>  to the end of the shortcut\'s Target.')
    print()
    print('  Saved in     %s' % data)
    print('  Logs in      %s' % logs)
    print()
    print('  Close this window (or press Ctrl+C) to stop the server.')
    print('=' * 50)

    try:
        while not failed:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nStopped.')
        return 0
    stop('The server stopped unexpectedly: %s\nSee %s for details.'
         % ('; '.join(failed), logs))


if __name__ == '__main__':
    sys.exit(main())
