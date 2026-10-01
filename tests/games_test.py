"""TW05 game adverts, end to end over TCP against a real server process.

    python tests/games_test.py

alice advertises a game in a room (`gcre`), bob enters the room and searches
(`gsea`) and sees it as a `+agm`, joins it (`gjoi`), and both get `+mgm` with
two players and then `+ses` naming the pair as OPPO0/OPPO1 with the game's
PARAMS -- what TW05's 'play' handler (0x001C0F00) reads.  Then a second
advert is withdrawn (`gdel`) and must be deleted from the room's list.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
sys.path.insert(0, HERE)
import tagfield                                         # noqa: E402
import tourney_test as T                                # noqa: E402

T.PORT = 20296
ROOM = 'Stroke.T.East'
PARAMS = 'CR=7\nG=0\nS=30\nM=32802\n'


def tags_of(body):
    return tagfield.decode(body.rstrip(b'\0').decode('latin-1'))


def until(sock, kind, test=lambda t: True, tries=40):
    for _ in range(tries):
        got, _ident, body = T.read(sock)
        if got == kind:
            t = tags_of(body)
            if test(t):
                return t
    raise AssertionError('never saw the %s we wanted' % kind)


def main():
    tmp = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(SERVER, 'lobbyd.py'), '--host',
         '127.0.0.1', '--port', str(T.PORT), '--logfile', '', '--ping', '0',
         '--backup-keep', '0', '--db', os.path.join(tmp, 'tw05.db'),
         '--buddy-port', '0', '--open', '--relay', 'same'],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    fails = []
    try:
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', T.PORT), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        a = T.sign_in('alice', 'alicepass')
        b = T.sign_in('bob', 'bobpass')
        for s in (a, b):
            s.sendall(T.frame('move', {'NAME': ROOM}))
            T.expect(s, 'move')

        a.sendall(T.frame('gcre', {'NAME': "alice's Stroke Play", 'PASS': '',
                                   'PARAMS': PARAMS, 'MINSIZE': '2',
                                   'MAXSIZE': '2', 'CUSTFLAGS': '0',
                                   'SYSFLAGS': '262146'}))
        _i, body = T.expect(a, 'gcre')
        made = tags_of(body)
        if made.get('HOST') != 'alice' or made.get('COUNT') != '1':
            fails.append('gcre should answer with the game record: %r' % made)
        until(a, '+mgm', lambda t: t.get('COUNT') == '1')

        b.sendall(T.frame('gsea', {'START': '0', 'COUNT': '50', 'ASYNC': '1',
                                   'SYSFLAGS': '0', 'SYSMASK': '524288'}))
        _i, body = T.expect(b, 'gsea')
        if tags_of(body).get('COUNT') != '1':
            fails.append('gsea should count one game: %r' % tags_of(body))
        seen = until(b, '+agm', lambda t: 'NAME' in t)
        if seen.get('NAME') != "alice's Stroke Play" or seen.get('PARAMS') != PARAMS:
            fails.append('+agm should carry the advert: %r' % seen)

        b.sendall(T.frame('gjoi', {'NAME': "alice's Stroke Play", 'PASS': ''}))
        _i, body = T.expect(b, 'gjoi')
        for who, s in (('alice', a), ('bob', b)):
            m = until(s, '+mgm', lambda t: t.get('COUNT') == '2')
            if (m.get('OPPO0'), m.get('OPPO1')) != ('alice', 'bob'):
                fails.append("%s's +mgm should list alice then bob: %r" % (who, m))
            # An ADVERTISED match dials the game record's addresses, not
            # +ses's (LAN server, 2026-10-01): they must be the relay's too.
            got = [m.get(k) for k in ('ADDR0', 'ADDR1', 'LADDR0', 'LADDR1')]
            if got != ['127.0.0.1'] * 4:
                fails.append("%s's +mgm should send both to the relay: %r"
                             % (who, got))
            ses = until(s, '+ses')
            if (ses.get('OPPO0'), ses.get('OPPO1')) != ('alice', 'bob') or \
                    ses.get('PARAMS') != PARAMS or not ses.get('AUTH'):
                fails.append("%s's +ses is wrong: %r" % (who, ses))
            # Both come from 127.0.0.1 -- one address, like two consoles in
            # one house -- and neither has a LAN address to give, so both
            # are sent to the relay (twrelay), at the address they reached
            # the lobby on.
            if (ses.get('ADDR'), ses.get('ADDR0'), ses.get('ADDR1')) != \
                    ('127.0.0.1',) * 3:
                fails.append("%s should be sent to the relay: %r"
                             % (who, {k: ses.get(k) for k in
                                      ('ADDR', 'ADDR0', 'ADDR1')}))

        # The two consoles' match traffic, through the relay on UDP 3658:
        # each gets what the other sent, untouched.
        ua, ub = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                  for _ in range(2))
        for u in (ua, ub):
            u.bind(('127.0.0.1', 0))
            u.settimeout(2)
        relay = ('127.0.0.1', 3658)
        ua.sendto(b'alice hello', relay)
        time.sleep(0.2)
        ub.sendto(b'bob hello', relay)
        try:
            got_a = ua.recvfrom(2048)[0]
            ua.sendto(b'alice again', relay)
            got_b = ub.recvfrom(2048)[0]
        except socket.timeout:
            got_a = got_b = None
        print('relay:  alice got %r, bob got %r' % (got_a, got_b))
        if (got_a, got_b) != (b'bob hello', b'alice again'):
            fails.append('the relay should pass traffic both ways, got %r / %r'
                         % (got_a, got_b))
        for u in (ua, ub):
            u.close()

        # A $2,500 wager: alice (host, 70) beats bob (74).  Both consoles
        # report; the wager moves once.  Then bob spends $1,000 and tries to
        # spend more than he has.
        token = ses['AUTH']
        card = {'AUTH': token, 'DISC': '0', 'WAGER': '2500',
                'DONE0': '1', 'DONE1': '1', 'QUIT0': '0', 'QUIT1': '0',
                'HOLES0': '18', 'HOLES1': '18', 'TYPE0': '0', 'TYPE1': '0',
                'STROKES0': '70', 'STROKES1': '74'}
        # As TW05 does it: both stakes are taken as the match starts.
        for who, s in (('alice', a), ('bob', b)):
            s.sendall(T.frame('cusr', {'PERS': who, 'CMD': 'ded$$',
                                       'DEDAMT': '2500'}))
            T.expect(s, 'cusr')
        for who, s in (('alice', a), ('bob', b)):
            s.sendall(T.frame('rank', dict(card, REPT=who)))
            T.expect(s, 'rank')
        b.sendall(T.frame('cusr', {'PERS': 'bob', 'CMD': 'ded$$',
                                   'DEDAMT': '1000'}))
        spent = tags_of(T.expect(b, 'cusr')[1])
        if (spent.get('ERRCODE'), spent.get('MONEY')) != ('0', '6500'):
            fails.append('bob should have $10,000 - 2,500 - 1,000 = $6,500: %r'
                         % spent)
        b.sendall(T.frame('cusr', {'PERS': 'bob', 'CMD': 'ded$$',
                                   'DEDAMT': '9999'}))
        over = tags_of(T.expect(b, 'cusr')[1])
        if over.get('ERRCODE') == '0' or over.get('MONEY') != '6500':
            fails.append('spending more than he has must be refused: %r' % over)

        # The game's own mode (PARAMS G) is what the match is filed as.
        import twdb
        db = twdb.DB(os.path.join(tmp, 'tw05.db'))
        row = db.one('SELECT setup FROM sessions')
        if not row or '"MODE": "0"' not in row['setup'] or '"COUR": "7"' not in row['setup']:
            fails.append('the session should record MODE 0 and course 7: %r'
                         % (row and row['setup']))
        if '"GAMEBITS": "32802"' not in row['setup']:
            fails.append('the session should keep the mode flags M: %r'
                         % row['setup'])
        ledger = {r['persona']: r['n'] for r in db.query(
            "SELECT persona, SUM(amount) AS n FROM cash"
            " WHERE kind IN ('stake', 'wager') GROUP BY persona")}
        if ledger != {'alice': 2500, 'bob': -2500}:
            fails.append('the winner should collect the $5,000 pot, once: %r'
                         % ledger)
        if db.one("SELECT COUNT(*) AS n FROM cash WHERE kind = 'spend'")['n'] != 1:
            fails.append("bob's purchase after the match is a spend, not a stake")
        db.conn.close()

        # Each mode as seen live (2026-09-29).  Battle reports TYPE 1 like
        # Match; only its M flags tell them apart.
        import twrecords
        for bits, typ, want in (('32802', 0, 'stroke'), ('32801', 1, 'match'),
                                ('32816', 2, 'mini'), ('32868', 1, 'battle')):
            got = twrecords.match_kind({'setup': {'GAMEBITS': bits, 'MODE': '0'},
                                        'type': typ, 'room': 'Match.T.East'})
            if got != want:
                fails.append('M=%s TYPE=%d should be %s, got %s'
                             % (bits, typ, want, got))

        # A withdrawn advert leaves the list.
        a.sendall(T.frame('gcre', {'NAME': 'second', 'PARAMS': PARAMS,
                                   'MINSIZE': '2', 'MAXSIZE': '2'}))
        made = tags_of(T.expect(a, 'gcre')[1])
        until(b, '+agm', lambda t: t.get('NAME') == 'second')
        a.sendall(T.frame('gdel', {'FORCE': '0'}))
        T.expect(a, 'gdel')
        gone = until(b, '+agm', lambda t: t.get('IDENT') == made['IDENT']
                     and 'NAME' not in t)
        if set(gone) != {'IDENT'}:
            fails.append('a withdrawn advert should be IDENT alone: %r' % gone)
        a.close()
        b.close()
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)

    for f in fails:
        print('FAIL', f)
    if fails:
        sys.exit(1)
    print('ok: advertise, search, join and start a match (through the relay,\n'
          '    as two consoles on one address); a withdrawn advert\n'
          '    is deleted from the room')


if __name__ == '__main__':
    main()
