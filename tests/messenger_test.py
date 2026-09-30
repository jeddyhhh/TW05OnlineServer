"""EA Messenger for TW05: keys, search, lists, presence and messages.

    python tests/messenger_test.py

The TW04 server's Messenger test, moved to TW05's frames:
the sign-in TW05 sends, then `EPGT` (answered with its ID) and the user
search `USCH` that TW05's add-a-buddy screen runs before `RADD`.

The Messenger client logs in with nothing but the `LKEY` its lobby connection
was given at `pers` (section 57), so the key is the identity.  This starts a
real server with `--buddy-port`, signs two consoles into the lobby, follows the
address `news NAME=0` hands out, and logs both in to Messenger.  Then it walks
what the EA Messenger screen does: add a buddy, see them come online, message
them, log in again and find the list kept, block and unblock, see them leave,
remove them -- and checks that a guessed key, a missing one, and a key a newer
sign-in replaced are all turned away.

The frame shapes are the ones TW05 consoles sent (2026-09-29).
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
import eacrypt                                          # noqa: E402
import tagfield                                         # noqa: E402
import twdb                                             # noqa: E402
from lobbyd import HDR, SESSION_KEY, cc, cc2i           # noqa: E402

PORT = 20293
BUDDY_PORT = 20294
TEST_DB = os.path.join(tempfile.gettempdir(), 'tw05-test-messenger.db')
PLAYERS = [('alice', 'alicepass'), ('bob', 'bobpass')]
STAT = 'en="TIGER WOODS PGA TOUR 2005"\nEX=0\nP=tw05\n'


def frame(kind, tags, ident=0):
    payload = tagfield.encode(tags).encode('latin-1') + b'\0'
    return struct.pack('>III', cc2i(kind), ident, HDR + len(payload)) + payload


# Bytes read past the end of a frame, per socket.  Two frames sent back to
# back -- `RGET` then its `ROST`s -- can arrive in ONE recv, and a reader that
# drops the tail loses the second: an intermittent timeout that only shows up
# when the server is fast enough to coalesce them.
LEFTOVER = {}


def read(sock):
    buf = LEFTOVER.pop(sock, b'')
    while len(buf) < HDR:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError('server closed early')
        buf += chunk
    kind, ident, size = struct.unpack('>III', buf[:HDR])
    while len(buf) < size:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError('server closed early')
        buf += chunk
    if len(buf) > size:
        LEFTOVER[sock] = buf[size:]
    return cc(kind), ident, buf[HDR:size]


def tags_of(body):
    return tagfield.decode(body.rstrip(b'\0'))


def expect(sock, kind):
    """Read frames until `kind` arrives, ignoring pushes along the way."""
    for _ in range(20):
        got, ident, body = read(sock)
        if got == kind:
            return ident, body
    raise AssertionError('never saw a %r reply' % kind)


def sign_in(name, password):
    """Log in to the lobby; return the socket and the LKEY `pers` issued."""
    sock = socket.create_connection(('127.0.0.1', PORT), timeout=5)
    sock.sendall(frame('skey', {'SKEY': b'Public Key'}))
    expect(sock, 'skey')
    ct = '~' + eacrypt.encode(password, SESSION_KEY).decode('latin-1')
    sock.sendall(frame('auth', {'NAME': name, 'PASS': ct, 'TOS': '1'}))
    ident, _ = expect(sock, 'auth')
    assert not ident, 'auth for %s failed: %s' % (name, cc(ident))
    sock.sendall(frame('pers', {'PERS': name}))
    ident, body = expect(sock, 'pers')
    assert not ident, 'pers for %s failed: %s' % (name, cc(ident))
    return sock, tags_of(body).get('LKEY', '')


def buddy_auth(key):
    """One Messenger login attempt, exactly as captured from a console."""
    sock = socket.create_connection(('127.0.0.1', BUDDY_PORT), timeout=5)
    tags = {'PROD': 'Tiger 2005', 'VERS': '1.0',
            'PRES': 'TIGER WOODS PGA TOUR 2005',
            'USER': '/CSO/TIGER-CONSOLE-2005/ntsc'}
    if key is not None:
        tags['LKEY'] = key
    sock.sendall(frame('AUTH', tags))
    ident, _ = expect(sock, 'AUTH')
    return sock, ident


def messenger(key, who):
    """A signed-in Messenger connection that has stated its presence."""
    sock, ident = buddy_auth(key)
    assert not ident, 'Messenger login failed: %s' % cc(ident)
    sock.sendall(frame('PSET', {'SHOW': 'CHAT', 'PROD': '%s is online' % who,
                                'STAT': STAT}))
    ident, _ = expect(sock, 'PSET')
    assert not ident, 'PSET should be acknowledged'
    return sock


def rget(sock, list_, rid, pend=False):
    """Fetch a list; return [(user, group)] -- or, with `pend` (TW05's
    PEND=Y), [(user, attr)] with the friend requests in it.  Presence that
    follows is left in the socket for `presence` to find."""
    tags = {'LRSC': 'CSO', 'LIST': list_, 'PRES': 'Y', 'ID': str(rid)}
    if pend:
        tags['PEND'] = 'Y'
    sock.sendall(frame('RGET', tags))
    _, body = expect(sock, 'RGET')
    size = int(tags_of(body).get('SIZE', '0'))
    rows = []
    for _ in range(size):
        _, body = expect(sock, 'ROST')
        t = tags_of(body)
        assert t.get('ID') == str(rid), 'ROST ID should name the list: %r' % t
        rows.append((t.get('USER'), t.get('ATTR', '') if pend
                     else t.get('GROUP', '')))
    return rows


def rnot(sock, who, timeout=2.0):
    """The next RNOT about `who`, as (CHNG, ATTR); None if none comes."""
    sock.settimeout(timeout)
    try:
        for _ in range(20):
            kind, _, body = read(sock)
            t = tags_of(body)
            if kind == 'RNOT' and t.get('USER') == who:
                return t.get('CHNG'), t.get('ATTR', '')
    except socket.timeout:
        return None
    finally:
        sock.settimeout(5)
    return None


def request_flow(key_a, key_b, fails, socks):
    """TW05's friend requests: RADM, answered with RRSP or withdrawn with
    RDEM, and both rosters kept in step with RNOT."""
    alice = messenger(key_a, 'alice')
    bob = messenger(key_b, 'bob')
    socks += [alice, bob]

    # 1. alice asks bob, in the wrong case
    alice.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'BOB', 'ID': '104',
                                 'PRES': 'Y'}))
    ident, body = expect(alice, 'RADM')
    t = tags_of(body)
    got = rnot(bob, 'alice')
    print('radm:   alice -> BOB: %s %s; bob told %r'
          % (cc(ident) if ident else 'OK', t, got))
    if ident or t.get('ID') != '104' or t.get('FUSR') != 'bob':
        fails.append('RADM should echo ID=104 and FUSR=bob, got %r' % t)
    if got != ('A', 'R'):
        fails.append("bob should be told of alice's request, got %r" % (got,))
    a_list, b_list = rget(alice, 'B', 1, True), rget(bob, 'B', 1, True)
    print('pend:   alice %r, bob %r' % (a_list, b_list))
    if a_list != [('bob', 'S')] or b_list != [('alice', 'R')]:
        fails.append('the request should be listed S for alice and R for bob')
    if rget(alice, 'B', 1) != [] or rget(bob, 'B', 1) != []:
        fails.append('a request must not show without PEND=Y')

    # 2. bob accepts: both are buddies, and alice's roster hears it
    bob.sendall(frame('RRSP', {'LRSC': 'CSO', 'ID': '5', 'USER': 'alice',
                               'ANSW': 'Y'}))
    ident, body = expect(bob, 'RRSP')
    got = rnot(alice, 'bob')
    print('accept: bob -> alice Y; alice told %r' % (got,))
    if ident or tags_of(body).get('ID') != '5':
        fails.append('RRSP should echo its ID')
    if got != ('A', ''):
        fails.append('an accept should reach alice as RNOT CHNG=A with no '
                     'ATTR, got %r' % (got,))
    if rget(alice, 'B', 1, True) != [('bob', '')] or             rget(bob, 'B', 1, True) != [('alice', '')]:
        fails.append('after an accept each should have the other as a buddy')

    # 3. bob removes alice: it goes both ways
    bob.sendall(frame('RDEL', {'LRSC': 'CSO', 'ID': '6', 'LIST': 'B',
                               'USER': 'alice'}))
    expect(bob, 'RDEL')
    got = rnot(alice, 'bob')
    print('remove: bob drops alice; alice told %r' % (got,))
    if got != ('D', '') or rget(alice, 'B', 1, True):
        fails.append('removing a buddy should take both off, got %r' % (got,))

    # 4. alice asks again; bob declines and blocks
    alice.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'bob', 'ID': '105',
                                 'PRES': 'Y'}))
    expect(alice, 'RADM')
    rnot(bob, 'alice')
    bob.sendall(frame('RRSP', {'LRSC': 'CSO', 'ID': '7', 'USER': 'alice',
                               'ANSW': 'B'}))
    expect(bob, 'RRSP')
    got = rnot(alice, 'bob')
    print('block:  bob -> alice B; alice told %r' % (got,))
    if got != ('D', '') or rget(bob, 'I', 2) != [('alice', '')]:
        fails.append('a decline-and-block should drop the request and put '
                     'alice on bob\'s ignore list, got %r' % (got,))
    alice.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'bob', 'ID': '106',
                                 'PRES': 'Y'}))
    ident, _ = expect(alice, 'RADM')
    got = rnot(bob, 'alice', timeout=0.5)
    print('block:  alice asks again -> %s; bob told %r'
          % (cc(ident) if ident else 'OK', got))
    if cc(ident) != 'blck' or got is not None:
        fails.append("a blocked request should come back 'blck' (which the "
                     "game treats as sent) and never reach bob")

    # 5. bob unblocks; alice asks and then withdraws
    bob.sendall(frame('RDEL', {'LRSC': 'CSO', 'ID': '8', 'LIST': 'I',
                               'USER': 'alice'}))
    expect(bob, 'RDEL')
    alice.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'bob', 'ID': '107',
                                 'PRES': 'Y'}))
    expect(alice, 'RADM')
    rnot(bob, 'alice')
    alice.sendall(frame('RDEM', {'LRSC': 'CSO', 'ID': '9', 'USER': 'bob'}))
    expect(alice, 'RDEM')
    got = rnot(bob, 'alice')
    print('recall: alice withdraws; bob told %r' % (got,))
    if got != ('D', '') or rget(bob, 'B', 1, True):
        fails.append('a withdrawn request should leave bob\'s roster, got %r'
                     % (got,))

    # 6. and a request crossing one the other already sent is a yes
    alice.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'bob', 'ID': '108',
                                 'PRES': 'Y'}))
    expect(alice, 'RADM')
    bob.sendall(frame('RADM', {'LRSC': 'CSO', 'USER': 'alice', 'ID': '109',
                               'PRES': 'Y'}))
    expect(bob, 'RADM')
    if rget(alice, 'B', 1, True) != [('bob', '')]:
        fails.append('crossing requests should make them buddies')
    for sock in (alice, bob):
        sock.close()
        socks.remove(sock)


def presence(sock, who, timeout=2.0):
    """The next PGET about `who`, skipping anything else; None if none comes."""
    sock.settimeout(timeout)
    try:
        for _ in range(20):
            kind, _, body = read(sock)
            t = tags_of(body)
            if kind == 'PGET' and t.get('USER') == who:
                return t
    except socket.timeout:
        return None
    finally:
        sock.settimeout(5)
    return None


def search(sock, text, maxr=20):
    """TW05's user search: the names the `USER` frames carry."""
    sock.sendall(frame('USCH', {'ID': '3', 'RSRC': 'CSO', 'USER': text,
                                'MAXR': str(maxr)}))
    ident, body = expect(sock, 'USCH')
    t = tags_of(body)
    assert not ident and t.get('ID') == '3', 'USCH reply: %r' % t
    names = []
    for _ in range(int(t.get('SIZE', '0'))):
        _, body = expect(sock, 'USER')
        u = tags_of(body)
        assert u.get('ID') == '3' and u.get('RSRC') == 'CSO', 'USER: %r' % u
        names.append(u.get('USER'))
    return names


def buddy_flow(key_a, key_b, fails, socks):
    alice = messenger(key_a, 'alice')
    socks.append(alice)

    # 0. TW05 asks EPGT straight after sign-in; the reply must carry ID=4
    alice.sendall(frame('EPGT', {'LRSC': 'CSO', 'ID': '4'}))
    ident, body = expect(alice, 'EPGT')
    t = tags_of(body)
    print('epgt:   %s %s' % (cc(ident) if ident else 'OK', t))
    if ident or t.get('ID') != '4' or t.get('ENAB') != 'F':
        fails.append('EPGT should answer ID=4 with ENAB=F, got %r' % t)
    rget(alice, 'B', 1)
    rget(alice, 'I', 2)

    # 0a. the search the add-a-buddy screen runs first
    for text, want in (('BO', ['bob', 'bobby']), ('bobby', ['bobby']),
                       ('ali', []), ('zz', []), ('%', [])):
        got = search(alice, text)
        print('usch:   %r -> %r' % (text, got))
        if got != want:
            fails.append('searching %r should find %r, got %r'
                         % (text, want, got))
    if search(alice, 'b', maxr=1) != ['bob']:
        fails.append('MAXR should cap the results')

    # 1. alice adds bob in the wrong case, while bob is offline
    alice.sendall(frame('RADD', {'LRSC': 'cso', 'ID': '101', 'LIST': 'B',
                                 'PRES': 'Y', 'USER': 'BOB', 'GROUP': ''}))
    ident, body = expect(alice, 'RADD')
    t = tags_of(body)
    print('radd:   BOB -> %s %s' % (cc(ident) if ident else 'OK', t))
    if ident or t.get('ID') != '101' or t.get('FUSR') != 'bob':
        fails.append('RADD should echo ID=101 and name the buddy FUSR=bob, '
                     'got %s %r' % (cc(ident) if ident else 'OK', t))

    # 2. nobody by that name
    alice.sendall(frame('RADD', {'ID': '102', 'LIST': 'B', 'USER': 'nobody'}))
    ident, body = expect(alice, 'RADD')
    print('radd:   nobody -> %s' % (cc(ident) if ident else 'OK'))
    if cc(ident) != 'user' or tags_of(body).get('ID') != '102':
        fails.append("a persona that does not exist should be refused with "
                     "'user' and its ID, got %r" % (cc(ident) if ident else 'OK'))

    # 3. bob comes on, and alice is told
    bob = messenger(key_b, 'bob')
    socks.append(bob)
    seen = presence(alice, 'bob')
    print('pget:   alice sees bob as %s' % (seen or {}).get('SHOW'))
    if not seen or seen.get('SHOW') != 'CHAT' or seen.get('PROD') != 'bob is online' \
            or seen.get('STAT') != STAT:
        fails.append("bob's PSET should reach alice verbatim as PGET, got %r"
                     % (seen,))

    # 4. a message, alice -> bob, arrives from alice with the body untouched
    body_in = 'encoded\x7ftext'
    # TW05 addresses it name/resource, as captured: USER=JeddyH/CSO
    alice.sendall(frame('SEND', {'TYPE': 'C', 'USER': 'bob/CSO',
                                 'BODY': body_in}))
    ident, _ = expect(alice, 'SEND')
    _, rbody = expect(bob, 'RECV')
    got = tags_of(rbody)
    print('recv:   bob got %r' % got)
    if ident or got.get('USER') != 'alice' or got.get('BODY') != body_in \
            or got.get('TYPE') != 'C':
        fails.append('a message should arrive as RECV USER=alice TYPE=C with '
                     'the body untouched, got %r' % (got,))

    # 5. the list survives a fresh login, with presence behind it
    alice.close()
    socks.remove(alice)
    alice = messenger(key_a, 'alice')
    socks.append(alice)
    rows = rget(alice, 'B', 1)
    seen = presence(alice, 'bob')
    print('relog:  list %r, bob %s' % (rows, (seen or {}).get('SHOW')))
    if rows != [('bob', '')]:
        fails.append('the buddy list should be kept, got %r' % (rows,))
    if not seen or seen.get('SHOW') != 'CHAT':
        fails.append('a list fetched with PRES=Y should be followed by the '
                     "buddies' presence, got %r" % (seen,))

    # 6. bob blocks alice: bob goes dark to her and she cannot message him
    bob.sendall(frame('RADD', {'LRSC': 'cso', 'ID': '7', 'LIST': 'I',
                               'PRES': 'Y', 'USER': 'alice'}))
    expect(bob, 'RADD')
    seen = presence(alice, 'bob')
    print('block:  alice sees bob as %s' % (seen or {}).get('SHOW'))
    if not seen or seen.get('SHOW') != 'DISC':
        fails.append('being blocked should make bob read as offline, got %r'
                     % (seen,))
    if rget(bob, 'I', 2) != [('alice', '')]:
        fails.append("the block should be on bob's ignore list")
    alice.sendall(frame('SEND', {'TYPE': 'C', 'USER': 'bob', 'BODY': 'hi'}))
    ident, _ = expect(alice, 'SEND')
    print('block:  alice -> bob -> %s' % (cc(ident) if ident else 'delivered'))
    if not ident:
        fails.append('a blocked sender should not be delivered')

    # 7. unblock, then bob leaves: alice is told he is offline
    bob.sendall(frame('RDEL', {'LRSC': 'cso', 'ID': '8', 'LIST': 'I',
                               'USER': 'alice'}))
    ident, body = expect(bob, 'RDEL')
    if ident or tags_of(body).get('ID') != '8':
        fails.append('RDEL should echo its ID')
    seen = presence(alice, 'bob')
    if not seen or seen.get('SHOW') != 'CHAT':
        fails.append('unblocking should make bob visible again, got %r' % (seen,))
    bob.close()
    socks.remove(bob)
    seen = presence(alice, 'bob')
    print('leave:  alice sees bob as %s' % (seen or {}).get('SHOW'))
    if not seen or seen.get('SHOW') != 'DISC':
        fails.append('bob disconnecting should reach alice as DISC, got %r'
                     % (seen,))

    # 8. a message to someone not on Messenger is refused, not dropped
    alice.sendall(frame('SEND', {'TYPE': 'C', 'USER': 'bob', 'BODY': 'hi'}))
    ident, _ = expect(alice, 'SEND')
    if not ident:
        fails.append('a message to an offline player should be refused')

    # 9. and alice takes him off the list
    alice.sendall(frame('RDEL', {'LRSC': 'cso', 'ID': '9', 'LIST': 'B',
                                 'USER': 'bob'}))
    expect(alice, 'RDEL')
    if rget(alice, 'B', 1):
        fails.append('RDEL should take bob off the list')


def start_server():
    return subprocess.Popen(
        [sys.executable, os.path.join(SERVER, 'lobbyd.py'),
         '--host', '127.0.0.1', '--port', str(PORT),
         '--buddy-port', str(BUDDY_PORT), '--logfile', '',
         '--ping', '0', '--backup-keep', '0', '--db', TEST_DB, '--open'],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def wait_up():
    for port in (PORT, BUDDY_PORT):
        for _ in range(80):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)


def main():
    for leftover in (TEST_DB, TEST_DB + '-wal', TEST_DB + '-shm'):
        try:
            os.remove(leftover)
        except OSError:
            pass
    setup = twdb.DB(TEST_DB)
    for name, password in PLAYERS:
        acct = setup.create_account(name, password, persona=name)
    setup.add_persona(acct, 'bobby')

    procs = [start_server()]
    fails = []
    socks = []
    try:
        wait_up()

        # the lobby issues a real key, different per login
        alice, key_a = sign_in(*PLAYERS[0])
        bob, key_b = sign_in(*PLAYERS[1])
        socks += [alice, bob]
        print('keys:   alice %s..., bob %s...' % (key_a[:8], key_b[:8]))
        for who, key in (('alice', key_a), ('bob', key_b)):
            if len(key) != 32 or ':' in key or key.startswith('lkey-'):
                fails.append('%s should get a 32-char minted key, got %r'
                             % (who, key))
        if key_a == key_b:
            fails.append('two logins must not share a key')

        # `news NAME=0` names the Messenger listener
        alice.sendall(frame('news', {'NAME': '0'}))
        _, body = expect(alice, 'news')
        addr = body.rstrip(b'\0').decode().strip()
        print('addr:   %s' % addr)
        if addr != '127.0.0.1:%d' % BUDDY_PORT:
            fails.append('news NAME=0 should hand out 127.0.0.1:%d, got %r'
                         % (BUDDY_PORT, addr))

        # VIEW RESUME on a buddy: `onln PERS=<name>` on the lobby, whose
        # reply TW05 keeps only when its `N` names that player
        alice.sendall(frame('onln', {'PERS': 'BOB'}))
        ident, body = expect(alice, 'onln')
        t = tags_of(body)
        print('onln:   BOB -> N=%r R=%r RP=%r, S %d chars'
              % (t.get('N'), t.get('R'), t.get('RP'), len(t.get('S', ''))))
        if ident or t.get('N') != 'bob' or not t.get('S') or 'RP' not in t:
            fails.append("onln should answer with bob's user record, N=bob, "
                         'got %r' % {k: v for k, v in t.items() if k != 'S'})
        alice.sendall(frame('onln', {'PERS': 'nobody'}))
        ident, _ = expect(alice, 'onln')
        if cc(ident) != 'user':
            fails.append('onln for a missing player should be refused')

        # FEEDBACK: `rept` with a TYPE.  Compliments are feedback, the rest
        # abuse reports that say what for; the reply is never read.
        for kind in ('honest', 'honest', 'goodsession', 'cheating'):
            alice.sendall(frame('rept', {'PERS': 'bob', 'LANG': 'en',
                                         'PROD': 'tigerntsc-ps2-2005',
                                         'TYPE': kind}))
            expect(alice, 'rept')
        check = twdb.DB(TEST_DB)
        praise = check.feedback_for('bob')
        reports = [(r['accused'], r['kind']) for r in check.reports()]
        print('rept:   bob praised %r, reports %r' % (praise, reports))
        if praise != {'honest': 1, 'goodsession': 1}:
            fails.append('compliments should count once per giver, got %r'
                         % praise)
        if reports != [('bob', 'cheating')]:
            fails.append('only the complaint should be a report, with its '
                         'kind, got %r' % reports)

        buddy_flow(key_a, key_b, fails, socks)
        request_flow(key_a, key_b, fails, socks)

        # guessed and missing keys are refused with the client's own codes
        for label, key, code in (('the old pattern', 'lkey-alice', 'auth'),
                                 ('a missing key', None, 'miss')):
            sock, ident = buddy_auth(key)
            sock.close()
            print('auth:   %s -> %s' % (label, cc(ident) if ident else 'OK'))
            if cc(ident) != code:
                fails.append('%s should be refused with %r, got %r'
                             % (label, code, cc(ident) if ident else 'OK'))

        # TW05 leaves the lobby when a match starts and signs back in to
        # Messenger mid-match with the key it already holds
        # (notes/tw05-games.md), so -- unlike TW04 -- a key outlives the
        # lobby connection it came from.  The next sign-in replaces it.
        bob.close()
        socks.remove(bob)
        time.sleep(0.3)
        sock, ident = buddy_auth(key_b)
        sock.close()
        print("auth:   bob's key after bob left the lobby -> %s"
              % (cc(ident) if ident else 'OK'))
        if ident:
            fails.append('a TW05 key must outlive its lobby connection, got %s'
                         % cc(ident))
        bob, key_b2 = sign_in(*PLAYERS[1])
        socks.append(bob)
        sock, ident = buddy_auth(key_b)
        sock.close()
        print("auth:   bob's old key after he signed in again -> %s"
              % (cc(ident) if ident else 'OK'))
        if cc(ident) != 'auth':
            fails.append('a new sign-in should replace the old key, got %r'
                         % (cc(ident) if ident else 'OK'))

        # ...but NOT with the server process.  alice is still signed in when
        # the lobby is killed and started again; her console reconnects to
        # Messenger with the key it already holds, and that has to work --
        # refusing it left real consoles with blank, frozen buddy lists.
        procs[0].terminate()
        procs[0].wait(timeout=5)
        procs[0] = start_server()
        wait_up()
        sock, ident = buddy_auth(key_a)
        rows = rget(sock, 'B', 1) if not ident else None
        sock.close()
        print("auth:   alice's key after a server restart -> %s, list %r"
              % (cc(ident) if ident else 'OK', rows))
        if ident:
            fails.append('a key must survive a server restart, got %s'
                         % cc(ident))
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=5)

    if fails:
        for f in fails:
            print('FAIL %s' % f)
        return 1
    print()
    print('ok: Messenger keys come from the lobby and a new sign-in replaces')
    print('    them; search finds players; friend requests are sent,')
    print('    accepted, declined and withdrawn; buddy and block lists are')
    print('    kept; presence, messages and blocks reach the right consoles')
    return 0


if __name__ == '__main__':
    sys.exit(main())
