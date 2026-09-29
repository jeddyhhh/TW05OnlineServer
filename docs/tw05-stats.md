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

`ONLINE RANK` comes from the user record's `R`. The RANK lines and EARNINGS RANK come from the rank requests, not the record.

**Not yet right:**

- DID NOT FINISH showed 92 under the probe, but reads 0 with 92=2 or 92=50.
- DNF LAST 10 showed 4 under the probe, but reads 1 for 4=2 and 3 for 4=7.

So both are derived from other fields; this needs a debugger look at the draw code. REP drew 0 and isn't placed yet.

Filled in from the database (`Handler.tw05_stats`), confirmed on JeddyH's screen:

- Stroke 1-0-0, Match 0-1, Mini 0-0-1 (Battle has no line)
- Points 3, Events 1
- `--probe-fields 92=50,...` overrides single fields for mapping.

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
