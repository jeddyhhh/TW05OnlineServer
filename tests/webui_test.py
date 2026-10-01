"""The TW05 web site, over HTTP against a real server process.

    python tests/webui_test.py

A fresh database with one account and a settled wager in the cash ledger,
then every public page must load without an error and speak TW05: the brand
and the patch download on the front page, the cash on the
leaderboard, a player's page and the account page, and a starting balance
taken from what lobbyd published.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
import twdb                                             # noqa: E402

PORT = 20297
SITE = 'http://127.0.0.1:%d' % PORT
PAGES = ('/', '/live', '/live.json', '/leaderboard', '/stats', '/records',
         '/courses', '/course/0', '/course/14', '/tournaments', '/halloffame',
         '/player/Alice', '/compare?a=Alice&b=Bob', '/h2h/Alice/Bob')


def get(path, opener=None):
    r = (opener or urllib.request.build_opener()).open(SITE + path)
    return r.status, r.read().decode('utf-8')


def main():
    tmp = tempfile.mkdtemp(prefix='tw05web')
    db_path = os.path.join(tmp, 'tw05.db')
    db = twdb.DB(db_path)
    acct = db.create_account('alice', 'pw12345', persona='Alice')
    db.add_persona(acct, 'Bob')
    # A $2,500 wager Alice won: both stakes taken, then the pot to her.
    db.add_cash('Alice', -2500, 'stake', 'm1')
    db.add_cash('Bob', -2500, 'stake', 'm1')
    db.add_cash('Alice', 5000, 'wager', 'm1')
    db.add_cash('Bob', 0, 'wager', 'm1')
    db.set_live('start_cash', 20000)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(SERVER, 'webui.py'), '--db', db_path,
         '--host', '127.0.0.1', '--port', str(PORT), '--no-secure-cookie',
         '--advertise', '127.0.0.1', '--logfile', '', '--quiet'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                get('/live.json')
                break
            except OSError:
                time.sleep(0.2)
        for path in PAGES:
            status, body = get(path)
            assert status == 200, (path, status)
            assert 'Traceback' not in body, path
        _, front = get('/')
        for want in ('PGA Tour 2005', '<code>127.0.0.1</code> port',
                     'SLUS-21002_88A808FA.pnach', '$20,000', 'Battle',
                     '3 Hole Mini-Game'):
            assert want in front, want
        _, patch = get('/SLUS-21002_88A808FA.pnach')
        assert 'patch=1,EE,001BDDF4,word,00002021' in patch
        # '127.0.0.1' over each host name, NUL-padded to the slot: the lobby
        # (both copies), Messenger and the demangler.
        for want in ('00361208,word,2E373231', '00361214,word,00000000',
                     '00363D38,word,2E373231', '0035F0F0,word,2E373231',
                     '0035F100,word,00000000', '003655D8,word,2E373231',
                     '003655E8,word,00000000'):
            assert 'patch=1,EE,' + want in patch, want
        assert patch.count('patch=1,') == 1 + 4 + 4 + 5 + 5
        # The same writes as real-PS2 cheat codes, behind the master code.
        for name in ('SLUS_210.02.cht', 'TW05-CheatDevice.txt'):
            assert '/' + name in front, name
            _, codes = get('/' + name)
            lines = codes.splitlines()
            assert lines[lines.index('Master Code') + 1] == \
                '90312F7C 0C0BBF8A', name
            assert '201BDDF4 00002021' in lines and \
                '20361208 2E373231' in lines, name
            assert sum(1 for l in lines if l.startswith('2')) == 1 + 18, name
        _, board = get('/leaderboard')
        assert '$22,500' in board and '$17,500' in board, 'cash board'
        try:
            get('/player/Nobody')
            raise AssertionError('a missing player must 404')
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        # Signed in: the account page carries the cash card and ledger.
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        r = op.open(SITE + '/login', urllib.parse.urlencode(
            {'account': 'alice', 'password': 'pw12345'}).encode())
        body = r.read().decode('utf-8')
        assert r.url.endswith('/account'), r.url
        assert [c.name for c in jar] == ['tw05'], 'cookie name'
        for want in ('<h2>Cash</h2>', '$22,500', '$17,500', '+$2,500',
                     '&minus;$2,500', 'Wagers and spending'):
            assert want in body, want
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)
    # --no-voice: one more patch line in each download, the voice branch.
    import webui
    webui.VOICE = False
    try:
        assert '201AEBE0 10000005' in webui.build_cht('127.0.0.1'), 'cht voice'
        assert '201AEBE0 10000005' in webui.build_cheatdevice('127.0.0.1')
        import tw05patch
        text = tw05patch.build_pnach('127.0.0.1', voice=False)
        assert 'patch=1,EE,001AEBE0,word,10000005' in text, 'pnach voice'
        assert 'patch=1,EE,001AEBE0' not in tw05patch.build_pnach('127.0.0.1')
    finally:
        webui.VOICE = True
    print('ok: every page loads; the patch carries the server\'s address over\n'
          '    every host name; cash shows on the leaderboard and account page')


if __name__ == '__main__':
    main()
