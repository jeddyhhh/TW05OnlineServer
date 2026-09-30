# TW05 player record, MY RESUME and online money (2026-09-29)

## The record

TW05's `S` is not TW04's. `twstats05.py` has the details:

- **94 bit-fields** (widths from the table at 0x0034A140, 1593 bits), laid end to end, little-endian.
- They sit in 217 bytes, **packed 7-into-8** so every byte is ≥ 0x80, giving 248 characters, then `!`.

## Where the console gets its own record: `+who`

- MY RESUME, the header in each room, and CURRENT CASH all read the console's **own** user entry, `self` (api+0x1558).
- That entry is filled by the **`+who`** push (handler 0x00327CAC, the same record parser as `+usr`: I N F A P S X R ...).
- TW04 never needed `+who`, so every TW05 stat read zero.
- The `onln` fallback (0x001C6A28) only runs when that entry is missing.
- The fork now sends `+who` after `pers`, after each head-to-head result and after each tournament round.

## MY RESUME, mapped live with `--probe-stats`

| Line | Field |
|---|---|
| ONLINE POINTS | 1 |
| MATCH PLAY W-L | 7, 8 |
| STROKE PLAY W-L-T | 10, 11, 12 |
| EVENTS ENTERED / WON / TOP 25 | 26, 27, 29 |
| TOURNAMENT EARNINGS ($) | 50 |
| MINI GAMES W-L-T | 57, 58, 59 |
| HANDICAP | 65 |
| **ONLINE EARNINGS = cash** | **76 (0x4C)** |
| WAGERS MADE / WON | 87, 88 |
| MONEY EARNED / LOST | 90, 91 |

`ONLINE RANK` comes from the user record's `R`.

**Solved 2026-09-30** with `lobbyd --probe-file`, bisecting the fields live:

| Line | Source |
|---|---|
| DID NOT FINISH | **field 15 + field 16 + field 61**: the incomplete games of match play, stroke play and the Mini-Game (Battle counts as match) |
| DNF LAST 10 GAMES | **the 1-bits of field 92**: a 10-bit history of the last ten games, 1 = did not finish (2 drew 1, 50 drew 3, 85 drew 4) |
| STROKE / MATCH / MINI GAMES RANK | **`cusr CMD=usrrk`**, whose reply is `RANKS`: 12 bytes, binary (`$` hex), three little-endian words in that order. `_GetUserRanksCallback` (0x001C2B40) stores them at lobby context +0x1E0. 0 draws N/A. |

The server fills these from the matches: per-mode quit counts, the last ten games' quits
as a bitmask (newest in bit 0), and `lobbyd.mode_ranks` (by wins, then fewest losses,
then most ties; equal records share a rank). Field 4, once taken for DNF LAST 10, is not it.

Things learned about the screen on the way:

- It takes the player's record once per visit; a `+who` sent while it's open shows next time.
- It refreshes when the game sends `myrnk`, which the game rate-limits to about one a
  minute or two.
- The game's own record has Win/Loss/Tie, Rank, Points, Status, Ping, **Comp, Incomp,
  DNF10**, AvgRnd and Earnings (a debug dump at 0x001C9594).

**REP and EARNINGS RANK, solved the same day.** Neither is in `S`:

- **REP** is the user record's **`RP`** tag. The record parser (0x003299C0) stores it at
  +0x210 and 0x001C6210 reads it for the screen; `RP=501` drew REP 501. The parser also
  takes `HW US MA LA AT CL LV MD WT WI G X`, not yet placed. The server sends REP as the
  percentage of head-to-head games the player finished rather than quit
  (`lobbyd.reputation`), 100 before any game.
- **EARNINGS RANK** is `myrnk`'s **`RNKRS` word 39**. TW05's RNKRS is 43 words, 172 bytes
  (decoded by 0x001C2930 into lobby context +0x1F0); the screen reads list 0x26 through
  0x001C6268, which 0x001C6D70 maps to word 39, and draws N/A when it is 0. TW04's was 36
  words with the rank in word 10, so the fork's 144-byte record never reached it.

Both confirmed on the console: JeddyH REP 67 (1 quit in 3 games), EARNINGS RANK 1.

Filled in from the database (`Handler.tw05_stats`), confirmed on JeddyH's screen:

- Stroke 1-0-0, Match 0-1, Mini 0-0-1 (Battle has no line)
- Points 3, Events 1
- `--probe-fields 92=50,...` overrides single fields for mapping; `--probe-file FILE`
  does the same (plus `ranks`, `rnkrs`, `R`) from a file re-read and re-sent each time
  MY RESUME opens, so the console never has to reconnect.

## Online money

- The **cash is field 0x4C** of that record. It shows as ONLINE EARNINGS on MY RESUME and as CURRENT CASH in game setup.
- `Lobby_DeductMyOnlineMoney` (`cusr CMD=... DEDAMT=n`) expects `ERRCODE` (0 = OK) and the new `MONEY`, which it writes into field 0x4C (0x001C2C58). That's how wagers and the Pro Shop spend it.
- The "$76,000 Pre-Round Total" on the tournament money screen is the **offline profile's** money, not the online cash.
- Currently the cash is set to the player's tournament earnings (finished days only). **How players should earn and spend online cash is an open design choice.**

## Online cash, as built (2026-09-29)

These are the operator's rules. **Balance = starting balance + tournament winnings + the `cash` ledger.**

- **Starting balance:** `--start-cash`, default $10,000.
- **Tournament winnings:** `tourney_career` earned, finished days only, the same figure as the money list. It's worked out, not stored.
- **The ledger:** a `cash` table in twdb, keyed on persona, kind and ref.
  - **Wagers** are settled from the head-to-head `rank` result, using `WAGER`, once per match `AUTH` (both consoles report).
  - The loser pays the winner, at most what the loser has.
  - Someone who quits while the other plays on forfeits. A tie moves nothing.
  - Both players get a fresh `+who` afterwards.
- **Spending:** `cusr CMD=ded$$ DEDAMT=n` (Lobby_DeductMyOnlineMoney, reached through a pointer, probably the Pro Shop).
  - It's refused with `ERRCODE=1` if the player can't afford it; otherwise `ERRCODE=0` and the new `MONEY`.
- **MY RESUME:**
  - ONLINE EARNINGS (field 0x4C) shows the balance.
  - WAGERS MADE / WON, MONEY EARNED / LOST come from the wager entries.
- **Seen live:** CURRENT CASH $10,000, and the WAGER setting un-greyed (it was grey at $0). It offers **$1,000, $2,000, $3,000…**
- **Not yet seen:** a real wager match. It should tell us whether `rank` `WAGER` is dollars (as `settle_wager` assumes) or a step number, and whether the game also calls `ded$$` when the match starts. The log line is `WAGER $n: loser pays winner`.
- `games_test.py` covers a $2,500 wager (settled once from two reports), a $1,000 spend, and a refused overspend.

## First real wager (2026-09-29): the game takes the stakes itself

A 9-hole Stroke match for $3,000. JeddyH won, 31 to 36.

- **`rank` `WAGER=3000` is dollars.**
- **As the match started, both consoles sent `ded$$ DEDAMT=3000`.** The game escrows each player's stake.
- Paying loser → winner on top of that charged the loser twice (JeddyB −$6,000) and gave JeddyH back only their own stake.

The fix:

- `ded$$` during a live, unreported match (`match_token`) is recorded as **kind `stake`** against the match's AUTH.
- The result pays the **pot** (the sum of the stakes) to the winner. A tie refunds each stake. If no stakes were taken, it falls back to a direct transfer.
- MY RESUME's wager lines use each match's net (stake + payout).

The recorded match was corrected by hand: JeddyH $13,000, JeddyB $7,000.

## Wager amounts

The WAGER setting cycles through a table at 0x0034F750 (29 entries; read by 0x00235A90):

    none, $1,000 .. $9,000 (by 1,000), $10,000 .. $90,000 (by 10,000),
    $100,000 .. $900,000 (by 100,000), $1,000,000

So the **maximum wager is $1,000,000**. Whether the menu hides amounts above the player's cash isn't confirmed. The server refuses a stake the player can't cover anyway (`ded$$` → ERRCODE 1).

Field widths to know:

- Cash (0x4C) is 32 bits, so up to $4.29 billion.
- **TOURNAMENTS EARNINGS (50) is only 25 bits: $33,554,431.**
- `twstats05.pack` now **clamps** to a field's width instead of wrapping, so a bigger total shows as $33,554,431, not a small number.
