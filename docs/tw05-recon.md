# Tiger Woods PGA Tour 2005 — first look at the online code

Static reading of the stock executable, 2026-09-28. Nothing here has been run
against a server yet. Where a claim is a guess, it says so.

- Game: Tiger Woods PGA Tour 2005 (USA), `SLUS-21002`. PCSX2 names it CRC
  **88A808FA**; its bundled `patches.zip` has a file under that CRC.
- ELF: `SLUS_210.02`, in the root of the disc. One `PT_LOAD` at
  0x00100000, file offset 0x1000, so **vaddr = file offset + 0xFF000**.
  `strings.py` prints file offsets.
- Course-mod ISOs (`TW05_POPP*`, `TW05_36*`) carry spliced
  ELFs, so a CRC-keyed `.pnach` for the stock game won't apply to them.
- Like TW04, the ELF keeps its debug strings, and `analysis/names.py` recovers
  77 function names from them (`Lobby_*`, `Tourn_*`, `_*Callback`).

## The lobby: same library, same protocol

TW05 is built on the same EA DirtySock LobbyAPI as TW04. Every request goes
through one routine:

    LobbyApiRequest(pApi, u4CC, pTagBuf, pCallback)   0x00326EC0

found from `Lobby_GetNews` (0x001CE3B8), which passes it `'news'` and
`_NewsCallback`. `analysis/requests_map.py` enumerates its 59 call sites into
`tw05-lobby-requests.txt`.

**Every verb TW04 uses, TW05 uses too:**

    acct auth chal cper cusr dper edit lost mesg move news onln pass peek
    pers rank rept room sele snap user

**TW05 adds six:**

| verb | caller | sends | what it looks like |
|---|---|---|---|
| `gcre` | 0x001BC348 → `_PostAdvertCallback` | NAME PASS PARAMS MINSIZE MAXSIZE CUSTFLAGS SYSFLAGS | post a game advert |
| `gsea` | 0x001BCAF0 | START COUNT ASYNC SYSFLAGS SYSMASK | search the adverts |
| `gjoi` | 0x001BC4F0 → `_JoinGameCallback` | | join an advertised game |
| `gdel` | 0x001BCC18, 0x001BCD10 → `_DeleteAdvertCallback` | FORCE | withdraw an advert |
| `glea` | 0x001BCE28 | | leave a game |
| `auxi` | 0x001C2FE8 | TEXT | auxiliary info text (guess) |

`sele` asks for `GAMES=1 MYGAME=1` as well as rooms, users and messages, so
the server is expected to push game adverts the way it pushes rooms.

**Login** sends TW04's fields plus `MAC`: `TOS NAME PASS MID MAC HWFLAG
HWMASK`, and reads `PERSONAS BORN SPAM CPAT`.

**The password cipher** should port unchanged: the ELF carries the same
CRC-32 table (0x00378A08) and `hello world` / `ru paranoid?` keys
(0x00378E08 / 0x00378E18) as TW04's (noted in the TW04 spec).

**Building the tag buffer differs.** TW04 calls TagFieldSetString/SetNumber
with a key and a value; TW05 formats lines from strings like `NAME=%d` (e.g.
`Lobby_GetNews` calls 0x002F12A0 with `"NAME=%d"`) or passes bare keys to its
own setters. On the wire it should be the same TagField text; the map lists
the keys each function references rather than exact values.

### `cusr` commands: renamed, and there are more

TW04's tournament commands were obfuscated; TW05's are plain words. Same
callbacks and same fields, so the pairing is by function:

| function | TW04 | TW05 | sends |
|---|---|---|---|
| Tourn_GetTodaysDateFromServer | `lts5d` | `date` | CMD |
| Tourn_GetTodaysTourneyInfoFromServer | `mg5ri` | `tinfo` | CMD START NUM |
| Tourn_GetNDaysTourneyInfoFromServer | `mg5ri` | `tinfo` | CMD START NUM LANG |
| Tourn_GetNDaysTourneyResultsFromServer | `qdb@w` | `tdwin` | CMD START NUM |
| Tourn_ReportRoundResults | `5d0tr` | `trslt` | PERS CMD DATA |
| _GetStartPermissionFromServer | `esr2t` | `tstrt` (+ `honest`) | reads TKEY DATA |
| my rank | `myrnk` | `myrnk` | |
| who am I | `whomi` | `whomi` | |

New in TW05, meaning not yet known: `usrrk` and `revoke`
(`Lobby_GetUserRanksByListIndices`), `logme` (0x001C64F8), `clrwg`
(0x001C784C), `prupd` / `prlog` (promo update / log? 0x001C8720,
0x001C8928), `tfrst` (0x001D5D84), and `Lobby_DeductMyOnlineMoney`, which
sends `CMD DEDAMT` and reads `ERRCODE MONEY`. So TW05 has server-held online
money. That may explain the `$25,000` on TW04's HUD.

## Where it connects

    Lobby_Init (0x001C3350):   host "ps2tw05.ea.com" (0x00361208), port "20200" (0x00361218)
    0x001CC56C (demo/auto):    host "ps2tw05.ea.com" (0x00363D38), port "20200" (0x00363D48)

**A hostname, not an IP**, unlike TW04's hard-coded `159.153.229.231:10200`. So
TW05 can be redirected three ways:

1. PCSX2's DNS override (Settings → Network & HDD → hosts):
   `ps2tw05.ea.com → <server IP>`. No game patch needed for the address.
2. A DNS server the PS2 is told to use. That is the real-console route, and
   needs no cheat device for the address.
3. Patch the two host strings to a dotted quad. There are 16 bytes
   (0x00361208–0x00361217), which fits any IPv4 address. `Lobby: Invalid IP
   address %s or port %u` suggests the connect path accepts one.
   **WORKS (2026-09-29, on the second machine with the host overrides
   removed):** the game connects with no DNS set-up at all. Messenger
   (`msgconn.beta.ea.com`, 0x0035F0F0, 20 bytes) and the demangler
   (0x003655D8, 20 bytes) are patched the same way, and so is the second
   `ps2tw05.ea.com` (0x00363D38). `webui.py` builds this patch per
   server (`HOST_SLOTS`, `host_patches`).

Other hosts in the ELF:

- `msgconn.beta.ea.com` (0x0035F0F0), with a `BUDDY_PORT` string: EA
  Messenger (buddy lists), as served by `lobbyd --buddy-port` for TW04.
- `demangler.ea.com` (0x003655D8, in 0x001D13C4): DirtySock's NAT
  "demangler". The log line `peer mangle approach success %s:%d after %d msec`
  means TW05 tries to find a peer's public address itself. TW04 has no
  equivalent; it may ease the UDP-3658 port-forward requirement, or fall back
  quietly if nothing answers.
- `GOLFVOIP: Connecting to: %s:%d:%d`: voice chat (headset). New in TW05.

**The first frame, `@tic RC4+MD5-V2`** (2026-09-30), is new in TW05. It
comes on the redirector connection, together with `@dir` (0x003271F8). It
offers to encrypt the lobby session: RC4 keyed with MD5, version 2. The
reply body goes straight to the session-key handler (0x00334898 posts it as
a `sess`/`keys` event).

A plain OK carries no keys, so the session stays in plain text like TW04's,
and every TW05 run has worked that way. `lobbyd.on_tic` declines it on
purpose. Encryption would add nothing, since `auth` already encrypts the
password, and it would make the logs unreadable.

## Master code (real PS2)

A real console's cheat engine (Open PS2 Loader, Cheat Device) needs a "9"
hook code: a `jal` the game makes every frame, and the instruction there.

- TW04's came from its CodeBreaker v1 master code, decrypted: `jal memcpy`
  at 0x001135AC inside libpad's `scePadRead`.
- TW05's published CodeBreaker master code (`B4336FA9 4DFEFB79` ...) is
  v7-encrypted; that first line is the encrypted `BEEFC0DE` key line.
- So it was found the other way round: TW05's `scePadRead` (libpad 2800) is
  at 0x00312E58, called only from the game's pad update (0x002D48F4). It
  does `memcpy(buf, pad, pad->len@0x60)` at **0x00312F7C = `jal 0x002EFE28`
  (0x0C0BBF8A)**, like TW04's.
- Confirmed in PCSX2's debugger, 2026-09-29: the breakpoint hits once a
  frame (about 4.7M EE cycles apart).

Master code: **`90312F7C 0C0BBF8A`**. `webui.py` builds the `.cht` and
Cheat Device files from it. NOT TRIED ON A REAL CONSOLE.

## DNAS

The DNAS glue is `golfdnas.c`. Its result handler is **0x001BDDAC**, and it
is TW04's pattern again:

    0x001BDDDC  lbu  $v0, -0x5f18($v1)     0x0034A0E8: DNAS has finished
    0x001BDDF4  lw   $a0, 0x29e8($v1)      0x003B29E8: gDNASOutputBlock.iResult
    0x001BDDFC  bnez $a0, <error paths>
                -- iResult == 0: "#### DNAS gDNASOutputBlock.iResult == 0"
    0x001BDE10  jal  0x00217878(0xBA)      the same 0xBA call TW04 makes on success
    0x001BDE24  sb   1, -0x5f15            0x0034A0EB: passed

The error paths print `#### DNAS ERROR: %s` with the error code, message and
footer, and 0x001CF648 then says `DNAS didn't succeed - returning to main
menu`.

**Candidate patch, UNTESTED:** make the load read zero, so a finished DNAS
run (which will fail, as there is no DNAS server) takes the success path:

    patch=1,EE,001BDDF4,word,00002021      // addu $a0, $zero, $zero  (was 8C6429E8)

It relies on DNAS *finishing* (the 0x0034A0E8 flag). TW04's module "fails in
the background" and finishes after a few seconds; if TW05's never finishes,
the screen will sit there, and the next place to look is whoever sets
0x0034A0E8.

## Code does not match across the two builds

`analysis/sigmatch.py` looks for a TW04 routine in TW05 by masked machine
code. Even LobbyApiRequest, the same library function, doesn't match:
TW05's frame is 0x2D0 where TW04's is 0x290, and the instruction scheduling
differs. It was a different compiler or different flags. Porting TW04
addresses to TW05 therefore goes through strings and names, not byte
patterns. sigmatch still self-matches within one build, which is useful for
checking a routine is unique.
