# TW05 tournament: date, season and calendar (2026-09-28)

Working in the server (a fork of the TW04 server), confirmed live in
PCSX2. The calendar shows a real generated month, and today's event opens
with the right course, purse and conditions.

## The three answers it needed

| Command | TW04 name | Reply | Why it mattered |
|---|---|---|---|
| `date` | `lts5d` | plain text, today's day number (days since 1899-12-30), e.g. `46293` | `_TodaysDateCallback` (0x001D48A0) runs atoi over the body into 0x0034AD98. A bare OK is day 0. |
| `tfrst` | `ufpvt` | plain text `first last daily weekly ceiling features`, the same six numbers as TW04 | Callback 0x001D4DC8 sscanfs `%d %d %d %d %d %d` into the season bounds and list indices. **Without it the calendar never asks for `tinfo`.** |
| `tinfo` | `mg5ri` | the hex list, TW05 layout below | `START` is the month's first day number, `NUM` the month's length. |
| `tdwin` | `qdb@w` | the results list, **unchanged from TW04** (12 data bytes, 0x2C stride) | Empty until someone finishes a round. |

The calendar only asks for `tinfo` when you press X on the CALENDAR tab.
Just arriving on the tab sends nothing.

## The TW05 calendar entry

The list format is TW04's (hex count, then per entry a hex name length, the
name raw, and hex data), but the entry grew: **60 data bytes**, 92 in memory,
104 at most (parser 0x001D4040).

The lookup (0x001D56D0) finds a day by the u16 at data 16 and range-checks
the first and last entry, so the list must stay sorted.

| Data bytes | Field | Source |
|---|---|---|
| 0–3 | purse, u32 | screen |
| 4–5 | u16 shown as `/ $n` after the headline | screen (unused, 0) |
| 6 | max attempts | accessor 0x001D6908 (0 shows "0"; meaning unknown) |
| 7 | forced golfer; **42 = none** (shows N/A), 0 = Tiger Woods | 0x001D6930, screen |
| 8–11 | u32: attributes (bits 0x8000/0x10000), difficulty (bits 0x20000–0x400000) | 0x001D6990, 0x001D6B78 (not tried) |
| 12–15 | **conditions u32** (below) | accessors + screen |
| 16–17 | **day**, u16 | lookup 0x001D56D0 |
| 18 | non-zero appends " (TP)" to the course | 0x001D6960 |
| 20–23 | **course code**: four characters as a big-endian number, stored little-endian (`PEBB` → `BBEP`) | 0x001D6910 → 0x00132320, screen |
| 28–59 | headline text (32 bytes) | screen |

**Conditions, u32 at data 12** (TW04's bits, all moved):

| Setting | Options |
|---|---|
| holes | Front 9 = 0x1, Back 9 = 0x2 |
| tees | White = 0x10 ✓, Blue = 0x20 ✓ |
| rough | Short = 0x4000 ✓, Long = 0x10000 ✓ |
| fairways | Medium = 0x40000 ✓, Fast = 0x80000 |
| greens | Slow = 0x800, Fast = 0x2000 ✓ |

✓ = seen on screen with `--probe-layout settings`; the rest are from the
game's code. The same word also has three more groups the event screen
doesn't show (0x100–0x400, 0x100000–0x4000000, 0x8000000–0x40000000): pins,
wind or weather, probably.

**Courses** are four-character codes. The game builds its course table from
the disc at run time (0x003832E8, 0x58 a record). Read live:

    0 PEBB Pebble Beach     5 CARI Paradise Cove   10 FANC Fancourt Links
    1 SAIN St Andrews       6 COLO Colonial CC     11 TURN Turnberry-Ailsa
    2 SAWG TPC at Sawgrass  7 COEU Coeur d'Alene   12 TROO Troon North Monument
    3 JAPA Emerald Dragon   8 SHER Sherwood CC     13 EDGE Edgewood GC
    4 GREE Greek Isles      9 HARB Harbour Town    14 ARCA Arcade Course

`COM1`–`COMA` and `DR18` (compilations and a Dream 18?) are handled
separately by 0x00132320, as indices 15–25.

## What changed in the fork

- **`twstats.COURSES`**: TW05's 15 courses.
- **`twtourney`**:
  - `DATA_BYTES`/`ENTRY_SIZE`/`DAY_OFFSET`, `CONDITIONS` and `CONDITIONS_OFFSET` for TW05
  - `COURSE_CODES`, `make_entry` writing the TW05 layout, and `entries_for` setting no forced golfer
  - `UNPLAYABLE_COURSES` = Arcade Course
  - `DIFFICULTY_ORDER` from `course_difficulty.json`'s tw05 averages, **not yet reviewed**; it sets the purses, from $5M at Paradise Cove down to $2M at Coeur d'Alene
- **`lobbyd`**:
  - answers `date` and `tfrst`; `tinfo`/`tdwin` replace `mg5ri`/`qdb@w`
  - `--probe-layout all|walk|settings` probes for mapping the entry
- The self-test (`python twtourney.py`) passes. **The TW04 test suites haven't been copied** into the TW05 server, and several would need TW05 courses.

## Playing an event: `tstrt` and `trslt` (2026-09-28)

**PLAY EVENT works live**: the server issues the key, the game accepts the
event and loads the course to the first tee.

- **`tstrt`** (TW04 `esr2t`), `_GetStartPermissionFromServer` 0x001D4C58:
  - It sends `PERS CMD` plus `PASS BIOT BIOL WRLD PROG SCEN` from 0x001D4A30.
  - Its callback `_TourneyStartCallback` (0x001D4590) reads `TKEY` (16 bytes
    binary, into 0x003B48E0) and `DATA`, one event in the calendar format,
    parsed by 0x001D4040 into 0x003B4880.
  - That's TW04's design, so `on_esr2t` answers it unchanged; the entry is
    now 60 bytes because it goes through the same `entries_for`.
- **`trslt`** (TW04 `5d0tr`), `Tourn_ReportRoundResults` 0x001D5E4C: the
  0x9C-byte struct above, as hex.
  - Its 28 words come from 0x003B37D0, which is also player 1's struct in the
    head-to-head `rank` report (`Lobby_SendTwoPlayerResults` 0x001CB618;
    player 2 is at 0x003B3840).
  - That function loads each value with `lw a3, off($s1)` just before its tag
    name, which names every word:

        0 RANK  1 DONE  2 TYPE  3 ?  4 ?  5 CLUB  6 QUIT  7 SCORE  8 ?  9 ?
        10 STROKES 11 PUTTS 12 HOLES 13 EAGS 14 BIRD 15 ACES 16 GIR 17 LPUT
        18 DRVS 19 FRWY 20 LDRV 21 SHTC 22 PARS 23 SBOG 24 DBOG 25 TBOG
        26 COMP 27 ?

    (TW04 had the 23 named ones back to back, in the order RANK DONE QUIT
    TYPE SCORE CLUB.) `twtourney.ROUND_WORDS`; the server logs every word of
    each report, including X3, X4, X8, X9 and X27.
- **The reply**: `_RoundResultsCallback` (0x001D4960) compares the body with
  "1" to "12". An exact match shows one of the game's own messages (text ids
  0x26A–0x275); anything else is shown verbatim. So TW04's plain-text reply
  still works.
- **Quitting sends nothing.** QUIT on the pause menu drops back to the online
  menu (and reconnects) without a `trslt`, so only a finished 18 holes reports.
- `tests/tourney_test.py` runs the whole chain against a real server
  process: date, season, calendar, start, report, a worse replay, and a wrong
  key. It passes.
- Found on the way, **in TW04 too**: `ensure_season()` sat inside
  `if ARGS.ping > 0`, so a server started with `--ping 0` had no calendar.
  Harmless in production (it always pings); fixed in the fork.

**Not yet seen: a real report from the game.** The layout and field order
come from the code, and the test builds reports in that layout, but no one has
finished an 18-hole TW05 tournament round against the server yet.

## Still open

- The calendar icon: every day draws a heart, and TW04's icon byte (15) has no TW05 equivalent yet.
- A real 18-hole round reported by the game (see above).
- The TW05 `snap` leaderboards and weekly money lists (empty so far).

## First real tournament round (2026-09-29)

JeddyH played the **Greek Isles Invitational** (White tees, Short rough, Slow fairways, Medium greens) and shot **62 (−10)**. The console showed the server's reply, "Your round of 62 is recorded. You are 1st of 1."

- `tstrt` issued the key. `trslt` came back 21 minutes later with that same key, today's day (46294) and the Greek Isles course code, and was filed against the event.
  - Strokes 62, par 72, place 1 of 1.
  - It's the round's first entry in `tourney` and `tourney_log`.
- **The report layout is confirmed.** Every named word is sensible:
  - 2 eagles, 10 birdies, 2 pars and 4 bogeys make 18 holes and 62 on par 72.
  - Putts 21, GIR 9, fairways 4 of 14, longest drive 356, longest putt 50.
- **Two more words, named (likely, from one round):**
  - word 3 = **4**, Greek Isles' course index (`CRSX`)
  - word 8 = **72**, the course's par (`PAR`)
  - Words 4 (= 1), 9 and 27 (= 0) are still unknown.
- The string after the struct is the **console's MAC address** (`$00041f82e366`).
- The money screen before it ("Pre-Round Total $76,000 … Grand Total") is TW05's own online money, earned in single-player modes. None of it is in `trslt`; it's held by the `cusr` money commands (`Lobby_DeductMyOnlineMoney`, `usrrk`?), which aren't answered yet.
