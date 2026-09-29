# TW05 master server

Everything needed to run the Tiger Woods PGA Tour 2005 lobby and its web site.
Nothing here needs anything installed beyond Python 3 -- no framework, no
database server, no build step. It can share a machine with the TW04 server:
the ports, the folder and the database are all its own.

|                     | TW04          | TW05          |
|---------------------|---------------|---------------|
| Lobby (TCP)         | 10200         | 20200         |
| EA Messenger (TCP)  | 10202         | 13505         |
| Web site (local)    | 8080          | 8081          |
| Web path            | `/TW04Online` | `/TW05Online` |

The lobby and Messenger ports are on the TW05 disc; they cannot be changed.

## Running it

    ADVERTISE=<public IP> ./tw05.sh start

`tw05.sh` starts the lobby and the web site, keeps them up, and writes one log
each to `logs/`. Both open the same SQLite file, `data/tw05.db`, created on
first run.

    ./tw05.sh start          start both; safe to run again, will not double-start
    ./tw05.sh start webui    just the one
    ./tw05.sh status
    ./tw05.sh restart [name]
    ./tw05.sh stop [name]
    ./tw05.sh log lobbyd     follow a log

Players register on the web site and sign in on the console with the same
account name and password. The personas on an account are the names offered on
the console's SELECT EA ACCOUNT screen.

### ADVERTISE: the address in every download

The site builds the PCSX2 patch (and the real-PS2 cheat files) for this
server: the DNAS bypass, and this server's IPv4 address written over the names
TW05 looks up. So the site must know the address players reach it by.
`ADVERTISE` is that address. Left empty, the site looks up the name it was
reached by -- on a server that name often resolves to 127.0.1.1 or a private
address, and every patch handed out would point there. **Set it**, at the top
of `tw05.sh` so cron starts it the same way.

If the server's address changes, players download the patch again.

### Other settings

At the top of `tw05.sh`, or in the environment:

- `START_CASH` (10000): the online cash every golfer starts with. Tournament
  prize money adds to it; head-to-head wagers move it between players.
- `WEB_HOST` (127.0.0.1), `WEB_PORT` (8081), `WEB_BASE` (/TW05Online): where
  the site listens. The defaults are for Apache in front, below.
- `SECURE_COOKIE` (1): the sign-in cookie is HTTPS-only. See "On a home
  network".
- `BUDDY_PORT` (13505): EA Messenger; empty to run without it. `BUDDY_ADDR`
  HOST:PORT only behind NAT.
- `DB` (data/tw05.db), `NEWS` (news.txt beside it), `LOG_MAX_MB`, `LOG_KEEP`,
  `PYTHON`.

On reboot, from `crontab -e` (the crontab of the user that owns the files, not
root's):

    @reboot /home/YOU/TW05Online/tw05.sh start

and optionally a watchdog, in case the supervisor itself is ever killed:

    * * * * * /home/YOU/TW05Online/tw05.sh start >/dev/null 2>&1

## Behind Apache, at a path

To serve the site at `https://example.com/TW05Online` -- beside
`/TW04Online` if that is there too -- run it on localhost (the default) and
point Apache at it. `a2enmod proxy proxy_http` if TW04 has not already, then
inside the existing `<VirtualHost *:443>`:

    ProxyPreserveHost On
    ProxyPass        /TW05Online  http://127.0.0.1:8081/TW05Online
    ProxyPassReverse /TW05Online  http://127.0.0.1:8081/TW05Online

(`ProxyPreserveHost On` is already there if TW04 is.) Then
`sudo apachectl configtest && sudo systemctl reload apache2`.

The path is passed through rather than stripped, and `--base-path` tells the
site to expect it, so every link and the sign-in cookie carry the prefix; the
cookie is called `tw05` and is not sent to the rest of the host. `tw05.sh`
passes `--trust-proxy`, so the login throttle sees each visitor's own address
rather than Apache's -- which is also why the site must listen on 127.0.0.1
only.

The game server is NOT proxied: consoles connect straight to TCP 20200, and
TCP 13505 for Messenger, so both have to be open on the firewall.

## On a home network

With no Apache and no HTTPS, let other machines reach the site and let the
sign-in cookie travel over plain http://:

    WEB_HOST=0.0.0.0 SECURE_COOKIE=0 ADVERTISE=192.168.1.x ./tw05.sh restart webui

and browse to `http://192.168.1.x:8081/TW05Online/`. Never on the internet:
without `Secure` one http:// link puts a live session token on the wire.

## Before it faces the internet

- Put TLS in front of the site (Apache, above).
- Do not run `lobbyd.py --open` (it creates an account on first login).
- Set `ADVERTISE`.
- The peer-to-peer leg of a match is UDP 3658 between the two players, not
  through this server. Each player needs that port reachable, which behind NAT
  means a port forward. Two copies of PCSX2 on one Windows PC cannot play each
  other.

## Managing accounts from the shell

    python3 twdb.py --list
    python3 twdb.py --create-account NAME --password PASS
    python3 twdb.py --add-persona ACCOUNT PERSONA
    python3 twdb.py --set-password ACCOUNT PASS
    python3 twdb.py --disable ACCOUNT

These open `data/tw05.db` beside them, the same file `tw05.sh` uses.

## The private pages

The site prints two unlinked addresses when it starts (`./tw05.sh log webui`),
kept in `data/`:

- `<site>/admin/<key>` (`data/admin.key`): password resets, bans, renames,
  deleting accounts, and the in-game news.
- `<site>/reports/<key>` (`data/reports.key`): REPORT ABUSE from the game,
  with the chat the lobby relayed in the hour before.

Anyone with an address can use it, so keep them to yourself; delete the file
and restart the site to change one.

This folder is generated by `TW05Online/sync_forserver.py` from
`TW05Online/server/`. Edit the originals there, not these copies.
