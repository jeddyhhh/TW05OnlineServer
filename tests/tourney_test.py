"""TW05 tournament, end to end over TCP against a real server process.

    python tests/tourney_test.py

Signs a console in and walks the tournament the way TW05 does:

    cusr date   -> today's day number, plain text
    cusr tfrst  -> the six season numbers
    cusr tinfo  -> this month's calendar in the 60-byte TW05 layout
    cusr tstrt  -> TKEY (16 bytes) and DATA, one entry for today
    cusr trslt  -> a 0x9C-byte round, signed with that TKEY

and then checks the round landed in the database against today's event, that
a worse replay keeps the best round, and that a round with the wrong key is
refused.  The report is built in the layout Tourn_ReportRoundResults
(0x001D5E4C) writes, not from the server's own parser, so a drift between
the two shows up here.
"""
import os
import shutil
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
import twtourney                                        # noqa: E402
from lobbyd import HDR, SESSION_KEY, cc, cc2i           # noqa: E402

PORT = 20295
LEFTOVER = {}


def frame(kind, tags, ident=0):
    payload = tagfield.encode(tags).encode('latin-1') + b'\0'
    return struct.pack('>III', cc2i(kind), ident, HDR + len(payload)) + payload


def read(sock):
    buf = LEFTOVER.pop(sock, b'')
    while len(buf) < HDR:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError('server closed early')
        buf += chunk
    kind, ident, size = struct.unpack('>III', buf[:HDR])
    while len(buf) < size:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError('server closed early')
        buf += chunk
    if len(buf) > size:
        LEFTOVER[sock] = buf[size:]
    return cc(kind), ident, buf[HDR:size]


def expect(sock, kind):
    for _ in range(40):
        got, ident, body = read(sock)
        if got == kind:
            return ident, body
    raise AssertionError('never saw a %r reply' % kind)


def cusr(sock, **tags):
    tags.setdefault('PERS', 'tess')
    sock.sendall(frame('cusr', tags))
    _ident, body = expect(sock, 'cusr')
    return body.rstrip(b'\0')


def sign_in(name, password):
    sock = socket.create_connection(('127.0.0.1', PORT), timeout=5)
    sock.sendall(frame('skey', {'SKEY': b'Public Key'}))
    expect(sock, 'skey')
    ct = '~' + eacrypt.encode(password, SESSION_KEY).decode('latin-1')
    sock.sendall(frame('auth', {'NAME': name, 'PASS': ct, 'TOS': '1'}))
    ident, _ = expect(sock, 'auth')
    assert not ident, 'auth failed: %s' % cc(ident)
    sock.sendall(frame('pers', {'PERS': name}))
    ident, _ = expect(sock, 'pers')
    assert not ident, 'pers failed: %s' % cc(ident)
    return sock


def report(key, day, course, strokes, bird, pars, sbog):
    """A round as TW05 builds it: 28 words, TKEY at 0x70, day at 0x80,
    course code at 0x84, a name at 0x89."""
    f = {'DONE': 1, 'STROKES': strokes, 'PUTTS': 30, 'HOLES': 18,
         'BIRD': bird, 'PARS': pars, 'SBOG': sbog, 'GIR': 12, 'DRVS': 14,
         'FRWY': 9, 'LDRV': 301, 'LPUT': 22}
    raw = bytearray(twtourney.ROUND_BYTES)
    struct.pack_into('<28I', raw, 0, *[f.get(n, 0) if n else 0
                                       for n in twtourney.ROUND_WORDS])
    raw[0x70:0x80] = key
    struct.pack_into('<H', raw, 0x80, day)
    struct.pack_into('<I', raw, 0x84, twtourney.course_code(
        twtourney.COURSE_CODES[course]))
    raw[0x89:0x8d] = b'tess'
    return raw.hex().upper()


def main():
    tmp = tempfile.mkdtemp()
    dbpath = os.path.join(tmp, 'tw05.db')
    proc = subprocess.Popen(
        [sys.executable, os.path.join(SERVER, 'lobbyd.py'), '--host',
         '127.0.0.1', '--port', str(PORT), '--logfile', '', '--ping', '0',
         '--backup-keep', '0', '--db', dbpath, '--buddy-port', '0', '--open'],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    fails = []
    try:
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', PORT), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        sock = sign_in('tess', 'tesspass')
        today = twtourney.today()

        got = cusr(sock, CMD='date')
        if got != str(today).encode():
            fails.append('date should be %d, got %r' % (today, got))

        season = cusr(sock, CMD='tfrst').split()
        if len(season) != 6 or not int(season[0]) <= today <= int(season[1]):
            fails.append('tfrst should be six numbers around today: %r' % season)

        first, length = twtourney.month_days(*twtourney.month_of(today))
        body = cusr(sock, CMD='tinfo', START=str(first), NUM=str(length),
                    LANG='en').decode('latin-1')
        cal = twtourney.decode_list(body)
        days = [struct.unpack_from('<H', d, twtourney.DAY_OFFSET)[0]
                for _n, d in cal]
        if days != list(range(first, first + length)):
            fails.append('tinfo should be the whole month, in order')

        sock.sendall(frame('cusr', {'PERS': 'tess', 'CMD': 'tstrt'}))
        _i, body = expect(sock, 'cusr')
        tags = tagfield.decode(body.rstrip(b'\0').decode('latin-1'))
        key = tags.get('TKEY')
        key = key.encode('latin-1') if isinstance(key, str) else key
        entry = twtourney.decode_list(tags.get('DATA', ''))
        if not key or len(key) != 16 or any(k == 0 for k in key[:1]) and \
                key == bytes(16):
            fails.append('tstrt should hand out a 16-byte key: %r' % key)
        if [e for e in entry] != [cal[today - first]]:
            fails.append("tstrt's entry should be today's calendar entry")
        event = twdb.DB(dbpath).event(today)

        got = cusr(sock, CMD='trslt', DATA=report(key, today, event['course'],
                                                  70, 4, 12, 2))
        if b'Your round of 70 is recorded' not in got:
            fails.append('the round should be recorded: %r' % got)
        got = cusr(sock, CMD='trslt', DATA=report(key, today, event['course'],
                                                  74, 2, 12, 4))
        if b'best of 70 still counts' not in got:
            fails.append('a worse replay should keep the 70: %r' % got)
        got = cusr(sock, CMD='trslt', DATA=report(bytes(range(16)), today,
                                                  event['course'], 60, 12, 6, 0))
        if b'not started on this server' not in got:
            fails.append('a round with the wrong key must be refused: %r' % got)

        db = twdb.DB(dbpath)
        row = db.one('SELECT * FROM tourney WHERE persona = ? AND day = ?',
                     ('tess', today))
        logged = db.one('SELECT COUNT(*) AS n FROM tourney_log '
                        'WHERE persona = ?', ('tess',))['n']
        if not row or row['strokes'] != 70 or row['course'] != event['course']:
            fails.append('the database should hold the 70 on today\'s course: '
                         '%r' % (dict(row) if row else None))
        if logged != 2:
            fails.append('two rounds should be logged, not %d' % logged)
        db.conn.close()
        sock.close()
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)

    for f in fails:
        print('FAIL', f)
    if fails:
        sys.exit(1)
    print('ok: date, season, calendar, start and report all work over TCP;\n'
          '    a worse replay keeps the best round and a wrong key is refused')


if __name__ == '__main__':
    main()
