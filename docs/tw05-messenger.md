# TW05 EA Messenger (2026-09-30)

TW05's Messenger has the same framing, sign-in (`AUTH` with the lobby's `LKEY`),
presence (`PSET`/`PGET`), lists (`RGET`/`ROST`) and messages (`SEND`/`RECV`) as
TW04's, and `lobbyd.BuddyHandler` already served those.

What TW05 adds is **user search** and **friend requests**. Both are now served,
and both have been confirmed on two consoles: JeddyH searched for "Jeddy",
asked JeddyB, and JeddyB accepted. The client's reply dispatcher is
0x003383F0; its requests are built between 0x003375A0 and 0x00337F10.

## Verbs the client knows

`AUTH RECV SEND BRDC PING ADMN RGET ROST RNOT PGET RADD RADM PADD PDEL RDEL
RDEM RRSP GRSP GRVK GINV GNOT EPST EPGT USCH USER DISC PSET`

## Answered now

| Verb | Request | Reply / push |
|---|---|---|
| `EPGT` | `LRSC ID=4`, straight after sign-in | `EPGT ID=4 ADDR= ENAB=F`. It must carry ID=4 (0x00338B98) or the request stays pending. `ADDR` + `ENAB=T` sets a flag; it looks like an address the account can be reached at. |
| `USCH` | `ID=3 RSRC=CSO USER=<text> MAXR=20` (0x00337E08) | `USCH ID=3 SIZE=n`, then n × `USER ID=3 USER=<name> RSRC=CSO` (0x00336D68 keeps 20; names are cut at `@ / .`). The game lists them (0x001BA100). |
| `RADM` | `LRSC USER ID PRES=Y`: a friend request | `RADM ID FUSR`, through the RADD reply code. An error of `blck` is treated as success. |
| `RRSP` | `LRSC ID USER ANSW` (0x00338E40); ANSW Y accept, N decline, B decline and block | `RRSP ID` |
| `RDEM` | `LRSC ID USER`: withdraw my request | `RDEM ID` |
| `RNOT` | pushed | `USER CHNG ATTR` (0x00336B90). CHNG=D removes the entry; CHNG=A adds or updates it. |

**`ATTR`** is a string of letters, one bit each (0x0032D210, table 0x00378748,
A=1 ... Z=26). The roster keeps three of them (0x003354F0):

- **S** = I asked them (roster flag 0x80000)
- **R** = they asked me (0x40000)
- **T** = unknown (0x1000000)

An entry that was S or R becomes a buddy when an `RNOT` arrives without that
letter. That's how the asker learns of an accept. `RGET ... PEND=Y` (TW05
always sends it for the buddy list) lists requests as ROSTs with ATTR S or R.

**Server model** (`twdb` list `'P'` = owner asked buddy):

- **Accept:** both become buddies, both rosters get `RNOT A`, and both hear
  each other's presence.
- **Decline:** the asker gets `RNOT D`.
- **Decline and block:** also puts the asker on the answerer's ignore list.
  After that, a request from the asker comes back `blck` and never arrives.
- **Crossed requests:** a request to someone who has already asked you counts
  as a yes.
- **Removing a buddy** (`RDEL LIST=B`) removes it on both sides, with
  `RNOT D` to the other console.

**On screen:** the three circles at the bottom right of the online menus are
indicators. The middle one flashes when a friend request is waiting.

## Messages, View Resume and Feedback

- **Messages** are TW04's `SEND`/`RECV`, except that TW05 addresses them as
  `name/resource` (captured: `USER=JeddyH/CSO`). The client's own parser
  (0x00335800) splits `name@domain/resource` into USER, DOMN and RSRC.
  `BuddyHandler.user_of` strips both parts for every verb that names a
  player. Before this, every message was refused as "not on EA Messenger".
  Confirmed both ways on two consoles.
- **View Resume** (the Messenger menu on a buddy) is a *lobby* request,
  `onln PERS=<name>`, sent by 0x0032E5B0. Its callback (0x0032E3C0) drops a
  reply whose `N` isn't the name it asked for, and TW04's reply had no `N`,
  so the screen sat on "Getting buddy profile from server". The reply is now
  the full user record (`lobbyd.user_record`, the same one as `+who`: I N P A
  R S RP). A failed reply falls back to `user PERS=` (callback 0x0032E2C8).
  Confirmed on the console.
- **Feedback** is the lobby's `rept` (TW04's REPORT ABUSE) with a `TYPE`
  (0x001C43FC):

  | Screen | TYPE |
  |---|---|
  | Good attitude | `honest` |
  | Great session | `goodsession` |
  | Bad name | `badname` |
  | Cheating | `cheating` |
  | Screaming | `screaming` |
  | Threats/harassment | `harassment` |
  | Cursing/lewdness | `language` |

  The two compliments are kept in the `feedback` table, one per giver per
  player per kind. The rest are abuse reports carrying their kind
  (`reports.kind`), shown on the operator's reports page. Before this, a
  "Good attitude" landed in the abuse queue.

## Challenge

**Challenge** on a buddy's Messenger menu doesn't use Messenger at all. It
is the lobby's challenge, TW04's `mesg TEXT=challenge ...`, then `accept`,
`chal PARAMS=...` from both, and `+ses`. It was captured 2026-09-30 from
JeddyB to JeddyH, and the server paired them. The consoles then failed with
"Could not send command packet", the known limit of two consoles on one PC
(notes/tw05-games.md). Not yet tried across two machines.

## Not answered yet (the catch-all sends an empty success)

- `GINV` (sent with `RSRC ID USER TITL SESS`, 0x00337A50), `GRSP`, `GRVK`,
  `GNOT`: game invitations through Messenger. The reply callbacks are type 7
  and 8 `ginv`. `GNOT TYPE HOST|USER` (0x003369A8) has four types: I (HOST
  invited me), A (accepted), D (declined), R (revoked). Challenge didn't use
  any of these, and nothing has sent one yet.
- `PADD` / `PDEL`: sent for roster flag 0x800000. It's not clear what sets
  that flag.
- `BRDC`, `ADMN`, `EPST`: broadcasts, admin messages, and the setter to go
  with `EPGT`.

`tests/messenger_test.py` covers everything in the table above over TCP.
