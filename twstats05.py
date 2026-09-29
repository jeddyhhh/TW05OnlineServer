"""TW05's player record: the `S` of a `+usr` push (and of an `onln` reply).

Read from SLUS_210.02, 2026-09-29.  It is NOT TW04's escaped-field record
(twstats.py):

  * 94 fields, each an unsigned bit-field of the width in the table at
    0x0034A140 (16 bytes an entry, width first), laid end to end in a
    little-endian bit stream -- field n starts at the sum of the widths
    before it, bit 0 first (getter 0x001C1538, setter 0x001C1868).  1593
    bits, in a 217-byte (0xD9) buffer.
  * The 217 bytes are packed 7 into 8 so that every byte has its top bit set
    (packer 0x001C13D8, unpacker 0x001C14A8): per group of seven, a flag byte
    0x80 | (bit 7 of byte k << k), then each byte as 0x80 | (byte & 0x7F).
    31 groups, 248 characters.
  * A '!' follows (0x001C6B08 checks record+0x120, which is character 248).
    An all-zero record is 248 x 0x80 and '!', which is what 0x001C1B68 builds.

Where it goes: the console looks up its OWN user entry ('self', 0x001C6AE8)
and copies that `S` into its stats (0x001C6B2C) when it is 248 characters
with the '!'; it only asks the server (`onln`) when it has no entry.  So the
`S` in `+usr` is what MY RESUME draws from.  Field 0x4C is the online cash
(_DeductMyOnlineMoneyCallback writes MONEY there, 0x001C2C58).
"""

# Bit width of each field, from 0x0034A140.
WIDTHS = (
    32, 17, 15, 10, 17, 17, 3, 13, 13, 13, 13, 13, 13, 8, 8, 11,
    11, 3, 3, 13, 13, 20, 20, 18, 17, 17, 10, 10, 10, 10, 17, 17,
    16, 17, 8, 17, 17, 9, 9, 8, 13, 8, 7, 10, 7, 7, 7, 16,
    1, 1, 25, 18, 8, 16, 20, 17, 17, 13, 13, 13, 8, 11, 3, 13,
    8, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 32, 32, 12, 128,
    32, 12, 128, 32, 12, 128, 16, 13, 13, 13, 32, 32, 10, 11,
)
FIELDS = len(WIDTHS)                     # 94
RAW_BYTES = 0xD9                         # 217
ENCODED = 248
MARKER = '!'

CASH = 0x4C                              # online money, $ (32 bits)

# MY RESUME, mapped live with --probe-stats (2026-09-29): each line drew the
# index of the field behind it.  ONLINE RANK is the user record's `R`, and
# the RANK lines and EARNINGS RANK come from the rank requests, not here.
POINTS = 1              # ONLINE POINTS
MATCH_W, MATCH_L = 7, 8                        # MATCH PLAY RECORD (W-L)
STROKE_W, STROKE_L, STROKE_T = 10, 11, 12      # STROKE PLAY RECORD (W-L-T)
EVENTS_ENTERED, EVENTS_WON, TOP25 = 26, 27, 29  # TOURNAMENTS
TOURNEY_EARNINGS = 50   # TOURNAMENTS EARNINGS, whole dollars (25 bits)
MINI_W, MINI_L, MINI_T = 57, 58, 59            # MINI GAMES RECORD (W-L-T)
HANDICAP = 65           # HANDICAP (9 bits)
ONLINE_EARNINGS = CASH  # ONLINE EARNINGS -- the same field is the cash
WAGERS_MADE, WAGERS_WON = 87, 88
MONEY_EARNED, MONEY_LOST = 90, 91
# The two DNF lines are worked out by the game, not read straight off a
# field -- which is why a field set to 92 drew 92 under the full probe and
# 0 on its own.  Bisected with --probe-file on 2026-09-30:
#   DID NOT FINISH   = field 15 + field 16 + field 61: the INCOMPLETE games
#                      of match play, stroke play and the Mini-Game
#   DNF LAST 10      = the 1-bits in field 92, a 10-bit history of the last
#                      ten games (2 drew 1, 50 drew 3, 85 drew 4)
MATCH_INCOMPLETE, STROKE_INCOMPLETE, MINI_INCOMPLETE = 15, 16, 61
DNF_HISTORY = 92        # 10 bits, one per game, 1 = did not finish
# REP drew 0 even with every field its own number, so it is not in this
# record at all; nor is EARNINGS RANK (N/A throughout).  Both come from
# somewhere not yet found.

OFFSETS = []
_bit = 0
for _w in WIDTHS:
    OFFSETS.append(_bit)
    _bit += _w
del _bit, _w


def pack(values):
    """{field index: int} -> the 217 raw bytes.  Values are clamped to their
    width, so nothing can spill into the next field."""
    n = 0
    for index, value in values.items():
        if not 0 <= index < FIELDS:
            raise IndexError('TW05 has fields 0..%d, not %d' % (FIELDS - 1, index))
        # Clamped, not wrapped: a total past a field's width (tournament
        # earnings is 25 bits, $33,554,431) shows as the most the field
        # holds instead of rolling over to a small number.
        top = (1 << WIDTHS[index]) - 1
        n |= max(0, min(int(value), top)) << OFFSETS[index]
    return n.to_bytes(RAW_BYTES, 'little')


def unpack(raw):
    n = int.from_bytes(bytes(raw)[:RAW_BYTES], 'little')
    return {i: (n >> OFFSETS[i]) & ((1 << WIDTHS[i]) - 1) for i in range(FIELDS)}


def encode(raw):
    """217 bytes -> 248 bytes, every one >= 0x80 (0x001C13D8)."""
    raw = bytes(raw).ljust(RAW_BYTES, b'\0')
    out = bytearray()
    for g in range(0, RAW_BYTES, 7):
        chunk = raw[g:g + 7]
        flag = 0x80
        for k, b in enumerate(chunk):
            if b & 0x80:
                flag |= 1 << k
        out.append(flag)
        out.extend(0x80 | (b & 0x7F) for b in chunk)
    return bytes(out)


def decode(enc):
    """The inverse (0x001C14A8)."""
    enc = bytes(enc)
    out = bytearray()
    for g in range(0, len(enc), 8):
        flag = enc[g]
        for k, b in enumerate(enc[g + 1:g + 8]):
            out.append((b & 0x7F) | (0x80 if flag >> k & 1 else 0))
    return bytes(out[:RAW_BYTES])


def record(values=None):
    """The `S` value: 248 encoded bytes and '!', as bytes."""
    return encode(pack(values or {})) + MARKER.encode()


def probe():
    """Every field that can hold its own index holds it (masked otherwise),
    so MY RESUME names the field behind each line."""
    return record({i: i for i in range(FIELDS)})


def _selftest():
    fails = []
    if len(record()) != ENCODED + 1 or record() != b'\x80' * ENCODED + b'!':
        fails.append('an empty record should be 248 x 0x80 and "!"')
    vals = {i: (i * 2654435761) & ((1 << WIDTHS[i]) - 1) for i in range(FIELDS)}
    rec = record(vals)
    if unpack(decode(rec[:ENCODED])) != vals:
        fails.append('a full record did not round-trip')
    if min(rec[:ENCODED]) < 0x80:
        fails.append('every encoded byte must have its top bit set')
    if sum(WIDTHS) != 1593 or FIELDS != 94:
        fails.append('the table should be 94 fields, 1593 bits')
    one = unpack(decode(record({CASH: 76000})[:ENCODED]))
    if one[CASH] != 76000 or any(v for i, v in one.items() if i != CASH):
        fails.append('cash should land in field 0x4C alone')
    for f in fails:
        print('FAIL', f)
    if not fails:
        print('ok: 94 fields, 1593 bits, 217 bytes -> 248 characters + "!"; '
              'records round-trip')
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(_selftest())
