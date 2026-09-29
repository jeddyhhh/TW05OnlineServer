#!/bin/sh
#
# Start, stop and supervise the TW05 master server and its web site.
# TW04's tw04.sh with TW05's ports, database and options.
#
#   ./tw05.sh start          start both, restarting either if it exits
#   ./tw05.sh start webui    just the one
#   ./tw05.sh stop [name]
#   ./tw05.sh restart [name]
#   ./tw05.sh status
#   ./tw05.sh log lobbyd     follow a log
#
# For a reboot, add this to `crontab -e`:
#
#   @reboot /home/YOU/TW05Online/tw05.sh start
#
# and, if you want a belt as well as braces, a minute watchdog that starts
# anything not running and does nothing otherwise:
#
#   * * * * * /home/YOU/TW05Online/tw05.sh start >/dev/null 2>&1
#
# `start` is safe to run repeatedly: it will not start a second copy of
# something already running.  It can share a machine with tw04.sh: each keeps
# its pid files and logs in its own folder, and the ports do not overlap.
#
# POSIX sh, no dependencies. It is a supervisor rather than a launcher: each
# service runs inside a loop that restarts it if it dies, with a short pause so
# a service that fails instantly cannot spin.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN="$HERE/run"
LOGS="$HERE/logs"
PYTHON=${PYTHON:-python3}

# Edit these, or set them in the environment before calling.
WEB_PORT=${WEB_PORT:-8081}            # TW04's site has 8080
WEB_HOST=${WEB_HOST:-127.0.0.1}       # behind Apache; do not face the internet
WEB_BASE=${WEB_BASE:-/TW05Online}
# The sign-in cookie is marked Secure (HTTPS only), as it should be behind
# Apache with TLS.  On a home network over plain http://, browsers drop it
# and signing in silently fails -- set SECURE_COOKIE=0 there, along with
# WEB_HOST=0.0.0.0 so other machines can reach the site at all.
SECURE_COOKIE=${SECURE_COOKIE:-1}
# The address the site builds into its downloads (the PCSX2 patch and the
# real-PS2 codes): this server's public IPv4 address, or a name that resolves
# to it.  Empty means the name the site was reached by, which is right when
# the lobby and the site are the same machine and that name resolves to it.
ADVERTISE=${ADVERTISE:-}
# The lobby's port is on the disc beside its name: 20200.  Changing it here
# only makes a lobby the game will never dial.
LOBBY_PORT=${LOBBY_PORT:-20200}
# EA Messenger (buddy lists, presence, messages). TW05 dials 13505, and it has
# to be open on the firewall like the lobby's. Set BUDDY_PORT= (empty) to run
# without it. BUDDY_ADDR overrides the host:port handed to consoles -- only
# needed behind NAT, where this machine's own address is not the public one.
BUDDY_PORT=${BUDDY_PORT-13505}
BUDDY_ADDR=${BUDDY_ADDR:-}
# Online cash everyone starts with; the site reads it from the lobby.
START_CASH=${START_CASH:-10000}
# The database both share, and the in-game news the admin page edits.
DB=${DB:-$HERE/data/tw05.db}
NEWS=${NEWS:-$(dirname -- "$DB")/news.txt}

RESTART_DELAY=${RESTART_DELAY:-3}

# One log per service, logs/lobbyd.log and logs/webui.log, each rolled over at
# LOG_MAX_MB into .1, .2 ... keeping LOG_KEEP old copies -- so the most disk
# the logs can take is about LOG_MAX_MB x (LOG_KEEP + 1) each.
LOG_MAX_MB=${LOG_MAX_MB:-10}
LOG_KEEP=${LOG_KEEP:-3}

mkdir -p "$RUN" "$LOGS" "$(dirname -- "$DB")"

# ---------------------------------------------------------------------------

command_for() {
    case "$1" in
    lobbyd)
        cmd="$PYTHON $HERE/lobbyd.py --port $LOBBY_PORT --db $DB --news $NEWS --quiet"
        cmd="$cmd --start-cash $START_CASH"
        cmd="$cmd --logfile $LOGS/lobbyd.log --log-max-mb $LOG_MAX_MB --log-keep $LOG_KEEP"
        # lobbyd runs Messenger by default, so "off" has to be said out loud.
        cmd="$cmd --buddy-port ${BUDDY_PORT:-0}"
        [ -n "$BUDDY_ADDR" ] && cmd="$cmd --buddy-addr $BUDDY_ADDR"
        echo "$cmd"
        ;;
    webui)
        cmd="$PYTHON $HERE/webui.py --host $WEB_HOST --port $WEB_PORT --base-path $WEB_BASE"
        cmd="$cmd --db $DB --news $NEWS --lobby-port $LOBBY_PORT --trust-proxy --quiet"
        cmd="$cmd --logfile $LOGS/webui.log --log-max-mb $LOG_MAX_MB --log-keep $LOG_KEEP"
        [ -n "$ADVERTISE" ] && cmd="$cmd --advertise $ADVERTISE"
        [ "$SECURE_COOKIE" = 0 ] && cmd="$cmd --no-secure-cookie"
        echo "$cmd"
        ;;
    *)  echo "" ;;
    esac
}

running() {
    pid=$(cat "$RUN/$1.pid" 2>/dev/null || true)
    [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null
}

# The supervisor loop. Runs in the background; its own pid is what we track,
# so stopping it stops the restarting too.
supervise() {
    name=$1
    cmd=$(command_for "$name")
    while : ; do
        echo "--- $(date '+%Y-%m-%d %H:%M:%S') starting $name" >>"$LOGS/$name.log"
        # The service writes its own log (and rolls it over); --quiet keeps it
        # off stdout so nothing lands twice.  stdout/stderr still go to the
        # same file, for anything that dies before the log is open.
        # shellcheck disable=SC2086
        $cmd >>"$LOGS/$name.log" 2>&1 || true
        echo "--- $(date '+%Y-%m-%d %H:%M:%S') $name exited, restarting in ${RESTART_DELAY}s" \
            >>"$LOGS/$name.log"
        sleep "$RESTART_DELAY"
    done
}

start_one() {
    name=$1
    if running "$name"; then
        echo "$name already running (pid $(cat "$RUN/$name.pid"))"
        return 0
    fi
    # `setsid` detaches it from this terminal and from cron's session, so it
    # survives the shell that started it going away.
    if command -v setsid >/dev/null 2>&1; then
        setsid "$0" __supervise "$name" </dev/null >/dev/null 2>&1 &
    else
        nohup "$0" __supervise "$name" </dev/null >/dev/null 2>&1 &
    fi
    echo $! >"$RUN/$name.pid"
    sleep 1
    if running "$name"; then
        echo "$name started (pid $(cat "$RUN/$name.pid"))"
    else
        echo "$name failed to start -- see $LOGS/$name.log" >&2
        return 1
    fi
}

stop_one() {
    name=$1
    if ! running "$name"; then
        echo "$name not running"
        rm -f "$RUN/$name.pid"
        return 0
    fi
    pid=$(cat "$RUN/$name.pid")
    # Kill the process GROUP: the supervisor and the python it started. Killing
    # only the supervisor would leave the service running and unmanaged.
    kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    n=0
    while running "$name" && [ "$n" -lt 10 ]; do
        sleep 1
        n=$((n + 1))
    done
    if running "$name"; then
        kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        echo "$name did not stop, killed"
    else
        echo "$name stopped"
    fi
    rm -f "$RUN/$name.pid"
}

status_one() {
    if running "$1"; then
        echo "$1: running (pid $(cat "$RUN/$1.pid"))"
    else
        echo "$1: stopped"
    fi
}

# ---------------------------------------------------------------------------

# Every command takes an optional service name; with none it means both.
SERVICES=${2:-}
if [ -z "$SERVICES" ]; then
    SERVICES="lobbyd webui"
elif [ "$SERVICES" != "lobbyd" ] && [ "$SERVICES" != "webui" ]; then
    echo "$0: no such service: $SERVICES (lobbyd or webui)" >&2
    exit 2
fi

case "${1:-}" in
__supervise)                       # internal, used by start_one
    supervise "$2"
    ;;
start)
    for svc in $SERVICES; do start_one "$svc"; done
    ;;
stop)
    for svc in $SERVICES; do stop_one "$svc"; done
    ;;
restart)
    for svc in $SERVICES; do stop_one "$svc"; done
    for svc in $SERVICES; do start_one "$svc"; done
    ;;
status)
    for svc in $SERVICES; do status_one "$svc"; done
    ;;
log)
    # -F follows the NAME, so it carries on across a rollover.
    tail -F "$LOGS/${2:-lobbyd}.log"
    ;;
*)
    echo "usage: $0 {start|stop|restart|status} [lobbyd|webui]" >&2
    echo "       $0 log [lobbyd|webui]" >&2
    exit 2
    ;;
esac
