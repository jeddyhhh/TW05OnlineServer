"""A UDP relay for the head-to-head leg, for players who cannot reach each
other directly.

TW05 plays a match peer to peer on UDP 3658: each console sends from its own
port 3658 to the opponent's port 3658 (0x001D1628; the port is a constant,
0xE4A, both ends).  The lobby only says WHICH address to send to (`+ses`
ADDR0/ADDR1; each console dials its "oppo", logme `oppo=`).

Two consoles behind one router, playing through a server on the internet,
come from one public IP.  Sent that IP for each other, their traffic has to
leave the router and come straight back in on the same fixed port, and it
does not survive (2026-10-01: two PCSX2 PCs dropped on hole 2 every time).
So instead the lobby gives each of them THIS server's address, and this
relay, listening on UDP 3658, passes each packet on to the other console.
Each console only ever talks outwards to the server, which any router can do.

Telling the two apart: packets are matched to a match by the source IP
(registered from each player's lobby connection), and within a match

  * different IPs -- the source IP says which player it is;
  * the same IP   -- the router gives each console its own source PORT, so
    the relay keeps the (ip, port) pairs it has seen and forwards to the
    other one.  It does not need to know which is which: whatever one sends,
    the other gets.

Nothing in a packet is read or changed.  One relayed match per public IP at a
time (a second match from the same house replaces the first).

    python twrelay.py        # self-test: two consoles on one IP, and two apart
"""
import itertools
import socket
import threading
import time

PORT = 3658                     # the game's fixed peer-to-peer port
IDLE = 120.0                    # a match silent this long is forgotten
MAX_AGE = 4 * 3600.0            # and none outlives this
BUF = 2048


class Relay:
    def __init__(self, host='0.0.0.0', port=PORT, log=None):
        self.log = log or (lambda kind, text: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.matches = []       # newest last
        # Arrival order for the sources of a one-address match: the clock
        # alone can give two packets the same time on Windows.
        self.order = itertools.count()
        self.heard = set()      # sources already logged, so each logs once
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # -- the lobby's side ----------------------------------------------------

    def add(self, token, a, ip_a, b, ip_b):
        """Relay the match `token` between persona `a` (whose lobby
        connection came from `ip_a`) and `b` (from `ip_b`)."""
        now = time.time()
        with self.lock:
            self.matches = [m for m in self.matches
                            if not ({a, b} & set(m['players']))
                            and not ({ip_a, ip_b} & set(m['players'].values()))]
            self.matches.append({
                'token': token, 'players': {a: ip_a, b: ip_b},
                'same': ip_a == ip_b, 'seen': {}, 'by_player': {},
                'started': now, 'last': now, 'packets': 0, 'bytes': 0,
                'announced': False})
        self.log('***', '    relay: match %s, %s (%s) <-> %s (%s)%s'
                 % (token, a, ip_a, b, ip_b,
                    ' -- one address, told apart by port' if ip_a == ip_b
                    else ''))

    def end(self, persona, why=''):
        """Forget any relayed match `persona` is in."""
        with self.lock:
            gone = [m for m in self.matches if persona in m['players']]
            self.matches = [m for m in self.matches if m not in gone]
        for m in gone:
            self._summary(m, why or '%s is done' % persona)

    def active(self):
        with self.lock:
            return [dict(m) for m in self.matches]

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    # -- the packets ---------------------------------------------------------

    def _summary(self, m, why):
        self.log('***', '    relay: match %s over (%s) -- %d packet(s), %d '
                        'bytes passed in %ds'
                 % (m['token'], why, m['packets'], m['bytes'],
                    int(time.time() - m['started'])))

    def _find(self, ip):
        for m in reversed(self.matches):
            if ip in m['players'].values():
                return m
        return None

    def destination(self, src, now=None):
        """Where a packet from `src` = (ip, port) goes, or None -- and note
        that `src` is alive.  Split out from the socket loop for the test."""
        now = time.time() if now is None else now
        with self.lock:
            self._expire(now)
            m = self._find(src[0])
            if m is None:
                return None
            m['last'] = now
            if m['same']:
                seen = m['seen']
                seen[src] = next(self.order)
                # A router can move a console to a new port mid-match; keep
                # the two most recently heard from.
                if len(seen) > 2:
                    for old in sorted(seen, key=seen.get)[:-2]:
                        del seen[old]
                others = [s for s in seen if s != src]
                return max(others, key=seen.get) if others else None
            me = next(p for p, ip in m['players'].items() if ip == src[0])
            m['by_player'][me] = src
            other = next(p for p in m['players'] if p != me)
            return m['by_player'].get(other)

    def _expire(self, now):
        keep = []
        for m in self.matches:
            if now - m['last'] > IDLE or now - m['started'] > MAX_AGE:
                self._summary(m, 'idle' if now - m['last'] > IDLE else 'too old')
            else:
                keep.append(m)
        self.matches = keep

    def _run(self):
        while self.running:
            try:
                data, src = self.sock.recvfrom(BUF)
            except OSError:
                if not self.running:
                    return
                continue
            dest = self.destination(src)
            if src not in self.heard and len(self.heard) < 1000:
                # Each new source once: what reached the relay at all, and
                # whether it belongs to a match -- the first thing to know
                # when a relayed match passes nothing.
                self.heard.add(src)
                with self.lock:
                    m = self._find(src[0])
                if m is None:
                    self.log('!!!', '    relay: %d bytes from %s:%d, which is '
                                    'in no relayed match -- dropped'
                             % (len(data), src[0], src[1]))
                else:
                    self.log('***', '    relay: heard %s:%d for match %s%s'
                             % (src[0], src[1], m['token'],
                                '' if dest else ' (waiting for the other '
                                'console)'))
            if dest is None:
                continue
            try:
                self.sock.sendto(data, dest)
            except OSError:
                continue
            with self.lock:
                m = self._find(src[0])
                if m is None:
                    continue
                m['packets'] += 1
                m['bytes'] += len(data)
                first = not m['announced']
                m['announced'] = True
            if first:
                self.log('***', '    relay: match %s is flowing, %s:%d -> '
                                '%s:%d' % (m['token'], src[0], src[1],
                                           dest[0], dest[1]))


def _selftest():
    fails = []
    r = Relay('127.0.0.1', 0)
    try:
        def console():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(('127.0.0.1', 0))
            s.settimeout(2)
            return s

        # 1. Two consoles on ONE address (one house): told apart by port.
        a, b = console(), console()
        r.add('t1', 'alice', '127.0.0.1', 'bob', '127.0.0.1')
        a.sendto(b'hello from a', ('127.0.0.1', r.port))    # b unknown yet
        time.sleep(0.1)
        b.sendto(b'hello from b', ('127.0.0.1', r.port))
        got = a.recvfrom(BUF)[0]
        if got != b'hello from b':
            fails.append('a should get what b sent, got %r' % got)
        a.sendto(b'x' * 1200, ('127.0.0.1', r.port))
        got = b.recvfrom(BUF)[0]
        if got != b'x' * 1200:
            fails.append('b should get what a sent, untouched')
        # the router moves b to a new port: the relay follows it
        b2 = console()
        b2.sendto(b'b moved', ('127.0.0.1', r.port))
        if a.recvfrom(BUF)[0] != b'b moved':
            fails.append('a should hear b on its new port')
        a.sendto(b'to the new b', ('127.0.0.1', r.port))
        if b2.recvfrom(BUF)[0] != b'to the new b':
            fails.append('a should reach b on its new port')

        # 2. Two apart: the source address says who is who.
        m = r.destination
        r.add('t2', 'carol', '10.0.0.1', 'dave', '10.0.0.2')
        if m(('10.0.0.1', 5000)) is not None:
            fails.append('before dave is heard there is nowhere to send')
        if m(('10.0.0.2', 6000)) != ('10.0.0.1', 5000):
            fails.append('dave -> carol')
        if m(('10.0.0.1', 5000)) != ('10.0.0.2', 6000):
            fails.append('carol -> dave')
        if m(('10.9.9.9', 1)) is not None:
            fails.append('a stranger must be dropped')

        # 3. Ending a match stops it; a quiet one expires.
        r.end('carol')
        if m(('10.0.0.2', 6000)) is not None:
            fails.append('an ended match must stop relaying')
        if m(('127.0.0.1', 1), now=time.time() + IDLE + 1) is not None:
            fails.append('an idle match must expire')
    finally:
        r.close()
    for f in fails:
        print('FAIL %s' % f)
    if not fails:
        print('ok: one address told apart by port, two apart by address; '
              'a moved port is followed; strangers, ended and idle matches '
              'are dropped')
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(_selftest())
