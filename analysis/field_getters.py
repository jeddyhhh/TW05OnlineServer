"""List what each caller of a "get entry" routine reads from the entry.

For every call site of <getter>, follow the returned pointer ($v0, and any
register it is copied into) for a few dozen instructions and print each load
through it with its offset.  Entry offsets are printed as-is and, when
--base is given, as offsets into the data block too (offset - base).

usage: field_getters.py <elf> <hex getter> [--base 0x20] [--span 40]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ee                                               # noqa: E402
from xref import load                                   # noqa: E402

LOADS = {0x20: 'lb', 0x24: 'lbu', 0x21: 'lh', 0x25: 'lhu', 0x23: 'lw',
         0x27: 'lwu', 0x37: 'ld', 0x31: 'lwc1'}
NAMES = ['zero', 'at', 'v0', 'v1', 'a0', 'a1', 'a2', 'a3', 't0', 't1', 't2',
         't3', 't4', 't5', 't6', 't7', 's0', 's1', 's2', 's3', 's4', 's5', 's6',
         's7', 't8', 't9', 'k0', 'k1', 'gp', 'sp', 'fp', 'ra']


def follow(img, site, span):
    """Loads through the pointer a call at `site` returns."""
    held = {2}                       # $v0 after the call
    out = []
    va = site + 8                    # skip the call and its delay slot
    for _ in range(span):
        w = img.word(va)
        op, rs, rt, rd = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        imm = w & 0xFFFF
        imm = imm - 0x10000 if imm & 0x8000 else imm
        if op in LOADS and rs in held:
            out.append((va, LOADS[op], imm, NAMES[rt]))
            held.discard(rt)
        elif op == 0 and (w & 0x3F) in (0x21, 0x25, 0x2D) and rt == 0 and rs in held:
            held.add(rd)             # move rd, rs
        elif op == 0 and (w & 0x3F) in (0x21, 0x25, 0x2D) and rs == 0 and rt in held:
            held.add(rd)
        elif op == 3:                # another call: $v0 and temporaries die
            held -= {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25}
        elif op == 0 and (w & 0x3F) == 0x08:     # jr: end of function
            break
        else:
            dest = rd if op == 0 else rt
            held.discard(dest)
        va += 4
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    img, _consts, calls = load(sys.argv[1])
    getter = int(sys.argv[2], 16)
    base = int(sys.argv[sys.argv.index('--base') + 1], 16) if '--base' in sys.argv else None
    span = int(sys.argv[sys.argv.index('--span') + 1]) if '--span' in sys.argv else 40
    for site in calls.get(getter, []):
        fs = ee.func_start(img, site)
        loads = follow(img, site, span)
        text = ', '.join('%s +0x%X%s -> %s' % (
            op, off, '' if base is None else ' (data %d)' % (off - base), dest)
            for _va, op, off, dest in loads) or '-'
        print('func 0x%08x  call @0x%08x  %s' % (fs or 0, site, text))


if __name__ == '__main__':
    main()
