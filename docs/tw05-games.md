# TW05 head-to-head: game adverts (2026-09-28)

ONLINE GAME MODES works live, up to the peer-to-peer connection:

- the rooms list and PEEK
- entering a room
- advertising a game, which shows in the room's list
- a second player joining
- both players getting the match start

A real match needs two consoles, which hasn't been tried.

## TW05 doesn't challenge, it advertises

TW04 matched players with `chal`. TW05 has its own lobby flow instead:

1. Enter a room (`move`, as TW04). The game sends `auxi TEXT=H=0\nL=0` and
   then `gsea` every ~10 s while the room screen is up.
2. **CREATE GAME** (the SELECT button) opens GAME SETUP:
   - mode and course
   - ranked, shot clock, holes, difficulty, handicaps, wager, golfer, headset
   - tees, pins, greens, fairway, rough, password, approval

   ADVERTISE GAME sends **`gcre`**:
   `NAME="JeddyH's Stroke Play" PARAMS=CR=0\nG=0\nS=30\nM=32802\nCF=…
   MINSIZE=2 MAXSIZE=2 CUSTFLAGS=0 SYSFLAGS=262146`.
3. The room's "Advertised Games" list fills from **`+agm`/`+gam`** pushes,
   one game record each. It also lists your own game.
4. Another player picks it: **`gjoi NAME PASS`**.
5. Both consoles get **`+mgm`** (COUNT 2) and then **`+ses`**, and the host
   shows "Joining game (host)", the peer-to-peer stage.
6. Withdrawing sends `gdel`; leaving a game, `glea`.

### Records and pushes (from the library, SLUS_210.02)

The push kinds TW05's lobby library knows:

    +agm +bud +gam +mgm +msg +pop +rnk +rom +ses +snp +sst +usm +usr +uss +ust +who

- **`+agm` (0x00328DC8) and `+gam` (0x00328F78)** each carry one game.
  - IDENT and NAME add or update it (record parser 0x00329490).
  - IDENT alone deletes it.
  - Record keys: `IDENT NAME SELF HOST PARAMS ROOM CUSTFLAGS SYSFLAGS COUNT
    MINSIZE MAXSIZE NUMPART SEED WHEN AUTH`, and per player n,
    `OPIDn OPPOn ADDRn LADDRn MADDRn`.
  - We send both kinds, and the list shows each game once.
- **`+mgm`** becomes the front end's `game` event (0x001C225C). It's status
  only:
  - no HOST: the game was deleted
  - COUNT ≥ 2: "about to go in game"
  - COUNT 1 with someone else as host: they quit
- **`+ses`** becomes the `play` event (0x001C0F00), the match start as in
  TW04. TW05 reads **`OPPO0`/`OPPO1` and `ADDR0`/`ADDR1`** (host first) and
  `PARAMS`. TW04 read `OPPO`/`FROM`/`ADDR`; the fork sends both sets.
- **`cusr CMD=logme LOGTXT=…`**: the game reports a failed match start, e.g.
  `game=JeddyH's Stroke Play,host=192.168.1.50,oppo=0.0.0.0,advt=`. Now
  logged.

### What the fork does (`lobbyd.py`, "TW05 game adverts")

- **`gcre`**: one game per host, kept in `GAMES` with the host as player 0.
  Replies with the record, and pushes `+agm`/`+gam` to the room and `+mgm`
  to the host.
- **`gsea`**: replies `COUNT`, then pushes every game in the searcher's room.
- **`gjoi`**: checks the password and whether the game is full, and adds the
  player.
  - A full game (MINSIZE reached) starts: `MATCHED` and `PENDING_SETUP`
    (course from `PARAMS` CR) are set, the advert leaves the room's list, and
    `start_session` pushes `+ses`.
  - From there it's TW04's code: the match token, `sessions` and the `rank`
    result.
- **`gdel` / `glea`**, changing rooms and disconnecting all drop a host's
  game, or take a guest out of it.
- **Test:** `tests/games_test.py`. Two clients cover advertise, search,
  join and `+ses`, and a withdrawn advert is deleted from the room. It passes.
- **Live:** the advert shows in the real game's list. With a scripted second
  player (a script) joining, the console went to "Joining game
  (host)" and, when the script didn't answer, "Unable To Connect To Peer".

## `demangler.ea.com`: must be redirected

At match start TW05 looks up **`demangler.ea.com`** and opens **TCP 3658**
to it. That's EA's NAT helper ("peer mangle"). The real name still resolves,
**to a server that isn't EA's** (3.92.16.247 on 2026-09-28). So every
player's PCSX2 (or DNS) needs `demangler.ea.com` pointed at our server, like
`ps2tw05.ea.com`. The patch the web site hands out now writes the server's address over that name as well.

With the override, our server refuses TCP 3658 (nothing listens) and the
game retries every few seconds. **Unknown:** whether it then falls back to
dialling the peer's address directly, as TW04 always does, or needs a
working demangler. TW05 logs `peer mangle approach success %s:%d after %d
msec, %d attempts`, so the demangler is used when it answers. The first
two-console test will tell. If it's required, it's DirtySock's ProtoMangle
protocol, which the lobby would serve on TCP 3658.

## First two-console run (2026-09-29)

Two PCSX2 instances on one PC:

- **A**: the MCP build, on Wi-Fi, 192.168.1.50, persona JeddyH.
- **B**: a second PCSX2 install, on wired Ethernet, 192.168.0.5, persona **JeddyB** (a second persona on the JeddyH account).
- The lobby runs with `--host 0.0.0.0`.

**The whole lobby side works:**

1. A advertises a game.
2. B enters East and sees it, with details (course, holes, ranked, tees, pins...).
3. B picks it and gets ACCEPT MATCH / DECLINE.
4. After accepting, both get `+ses`: "Joining game (host)" on A, "Joining game (client)" on B.

**Crash found and fixed: the golfer blob.** When B entered the room, A asked for B's golfer (`user PERS=JeddyB`) and crashed: "Jump to unmapped recLUT page (PC: 0x3838b0b8)".

- The cause: TW05 sends `CRPIN` as raw bytes in its own 8-into-7 packing (every byte ≥ 0x80), and reads the reply raw.
  - 0x001C29C8 takes the value, `strlen`s it, and unpacks it into a 0x800-byte stack buffer, with no unescaping.
  - The server was sending it back as TW04's `$hex`, twice the length, which unpacked to ~2750 bytes and smashed the stack. The PC is hex digits from the reply.
- The fix: `tagfield.Raw`, so `on_user` returns the exact bytes the console uploaded, and refuses anything over 2340 bytes. Confirmed live: A then took B's golfer without trouble.

**The demangler is optional.**

- With `demangler.ea.com` pointed at us and nothing on TCP 3658, both consoles retry it for ~20 s, then **fall back to direct UDP 3658 peer-to-peer**, as TW04 does.
- `logme` reports what each saw:
  - A: `host=192.168.1.50,oppo=192.168.0.5`
  - B: `host=192.168.1.50,oppo=192.168.1.50`
- So the server needs no ProtoMangle. A demangler would only shorten that 20 s wait; not answering TCP at all (RST) is what makes the fallback quick.

**The peer link failed for a host reason, not a game one.**

- Both consoles bound UDP 3658 on their own adapter (no clash), then showed "Unable To Connect To Peer".
- A UDP check between the two adapters shows `BLOCK` both ways. That's Windows strong-host send/receive: a datagram from one local adapter to another local adapter's address is dropped (TW04 notes, section 21).
- To finish the test, either:
  - put B on a **second machine** (TW04's working setup), or
  - enable weak-host on both adapters:
    `netsh interface ipv4 set interface <WiFi idx> weakhostsend=enabled weakhostreceive=enabled`, and the same for Ethernet. That's a system networking setting, and the operator's call.

## First real match (2026-09-28, second machine)

B was moved to a second machine (192.168.1.13, on the same Wi-Fi network as A). The peer link connected and **both players teed off**, then quit. So TW05 head-to-head works end to end through this server.

- **Both consoles sent `rank`** with the server's `AUTH` and `DISC=1`, each stored.
  - JeddyH (host) has `QUIT0=1`, no holes done, so there's no winner.
  - The tee shots are there: `LDRV0=192`, `LDRV1=220`.
  - New fields compared with TW04: `RATING=74`, `WAGER=0`, `NAME0/1`, `GLFR0/1`, `CRSE0/1`, `ROOM0/1`, `MD5SUM`.
- **The `rank` fields are TAB-separated**, with an unquoted space in `WHEN=2026.9.28 5:29:46`. `tagfield._split` now splits a payload containing a tab on tabs and newlines only.
- **TW05 signs out of the lobby when a match starts** and reconnects to EA Messenger during play with the same LKEY. TW04's rule of retiring the key when the lobby connection closes got both consoles refused 12 s into the match. The fork no longer retires it; the next sign-in replaces it, and it expires after 12 h.

## First full 18-hole round (2026-09-28)

JeddyB hosted a Stroke game at Pebble Beach. Both consoles reported `DISC=0` with the same `AUTH`.

- **JeddyH 49 (−23) beat JeddyB 60 (−12).**
- Every scoring line adds up to 18 holes and to par 72.
- Stored with JeddyH as the winner.

**The game mode comes from PARAMS `G`, not the room.** It was filed as match play because TW04 decides by the room name (`Match.T.East`), but TW05 lets any mode be played in any room.

- `G` goes to 0x001CC160. The numbering matches `Online_SetMatchGamemode` (0x001D2E24) and the room-type names (0x0021B8D0): **0 = Stroke, 1 = Match**. 3, 0x19 and 0x1B also exist, unnamed (skins? other modes).
- `params_setup` stores it as setup `MODE`, and `twrecords.match_kind` uses it, falling back to the room name.
- The two recorded matches were corrected to MODE 0; both adverts had sent `G=0`.
- Not yet seen: a Match-play advert, to confirm `G=1`.

## First Match-play round (2026-09-29), confirms G=1

"JeddyH's Match Play" at Coeur d'Alene (`CR=7`) sent **`G=1`** (and `S=60`, `M=32801`). The session stored `MODE 1`, and the rounds are filed as `match`.

- **JeddyB won 8 & 7.** The match ended after 11 holes:
  - `SCORE` is holes won, JeddyB 9 and JeddyH 1, with one halved.
  - `TYPE0/1=1` (match), `STROKES=0`, as in TW04's match play.
  - `COMP` counts only the holes each player holed out (7 and 10).
- The time now arrives whole (`WHEN=2026.9.28 14:39:50`), from the tab fix.
- With the LKEY fix, there were no Messenger refusals during the match.

## 3 Hole Mini-Game (2026-09-29)

"JeddyB's 3-Hole Play": three random holes from random courses. It ended in a **10–10 tie**: 3 holes each, one birdie and two pars each.

- **The advert doesn't identify it by `G`.** It sent `G=0`, the same as Stroke, and `CR=0` (whatever the menu showed).
  - `M` differs: Stroke `M=32802` (0x8022), Match `32801` (0x8021), Mini-Game `32816` (0x8030).
  - Those low bits match `Online_SetMatchGamemode`'s flags (0x001D2E24): 0x02 Stroke, 0x01 Match, 0x10 for mode 0x1B.
- **The `rank` report does identify it:** `TYPE0/TYPE1` = 0 Stroke, 1 Match, **2 Mini-Game**.
  - `twdb` now keeps it as `match['type']`, and `twrecords.match_kind` uses it first, then MODE, then the room.
  - Unknown types become `typeN`, so Battle will show up as its own kind rather than as stroke play.
- **Mini-Game rounds have no course** (`course=None`), so random holes don't land on Pebble Beach's page. They're only 3 holes, so they never count toward scoring averages or par.
- **MY RESUME: BEST ROUND and SCORING AVERAGE now count 18-hole rounds only.** Before, the 3-hole 10 would have been a "best round of 10". **TW04's `stat_record` has the same gap** for any partial round.

## Battle (2026-09-29)

Match play in which winning a hole lets you remove a club from your opponent's bag, or put back one of yours. It was played for 2 holes and abandoned: both reports have `DISC=1`, and JeddyH, the host, has `QUIT0=1`. So it's stored, but it counts for nothing.

- **Battle is indistinguishable from Match everywhere but `M`.**
  - The advert sent `G=1` (like Match), and both `rank` reports say `TYPE=1` (like Match).
  - Only the advert's `M=32868` (0x8064) differs.
- **The `M` low five bits** are one flag per mode:
  - 0x01 Match, 0x02 Stroke, **0x04 Battle**, 0x10 Mini-Game, 0x08 not yet seen
  - 0x20 is always set; Battle also sets 0x40.
- The fork keeps `M` as setup `GAMEBITS` (`params_setup`), and `twrecords.match_kind` checks the Battle bit first, then TYPE, then the other bits, MODE and the room.
- The five matches already stored were backfilled from the log's `gcre` adverts.
- **Battle rounds are their own kind (`battle`).** MY RESUME's Match and Stroke lines don't include them.
- `games_test.py` checks all four modes as seen live.

**Kinds so far:** `stroke`, `match`, `mini` (course None), `battle`, `tourney`. Whether Battle and the Mini-Game should get their own records or leaderboards is a site decision for later.
