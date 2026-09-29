"""Find a TW04 function in another build (TW05) by its machine code.

The two games share EA's DirtySock library, compiled from nearly the same
source, so a library routine keeps its instruction stream between builds and
only its addresses move.  This takes the routine's first N instructions from
the reference ELF, masks everything that depends on where things were linked
-- jump/call targets, `lui` immediates, and anything addressed off $gp -- and
searches the other ELF's code for that pattern.

usage: sigmatch.py <reference elf> <target elf> <hex vaddr> [words]
       sigmatch.py <reference elf> <target elf> --table NAME=ADDR ...

Prints each match; one match is an answer, several mean the prefix is too
short (add words), none means the routine changed (try fewer words).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ee                                               # noqa: E402

GP = 28


def mask_of(w):
    """(value mask) keeping only the parts of `w` that survive relinking."""
    op = w >> 26
    rs = (w >> 21) & 31
    if op in (2, 3):                     # j / jal: target moves
        return 0xFC000000
    if op == 0x0F:                       # lui: upper half of an address
        return 0xFFFF0000
    if op >= 0x08 and rs == GP:          # anything off $gp
        return 0xFFFF0000
    if op in (0x09, 0x0D) and rs != 29:  # addiu/ori: often the low half of an address
        return 0xFFFF0000
    if op >= 0x20 and rs not in (29, 31):  # loads/stores off a register that may hold an address
        return 0xFFFF0000
    return 0xFFFFFFFF


def pattern(img, vaddr, n):
    words = [img.word(vaddr + 4 * i) for i in range(n)]
    return [(w & m, m) for w, m in ((w, mask_of(w)) for w in words)]


def search(img, pat):
    data = img.data
    first_val, first_mask = pat[0]
    hits = []
    end = len(data) - 4 * len(pat)
    for i in range(0, end, 4):
        w = struct.unpack_from('<I', data, i)[0]
        if w & first_mask != first_val:
            continue
        for k, (val, m) in enumerate(pat[1:], 1):
            if struct.unpack_from('<I', data, i + 4 * k)[0] & m != val:
                break
        else:
            hits.append(img.base + i)
    return hits


def find(ref, tgt, vaddr, words=None):
    """Grow the prefix until the match is unique (or give up)."""
    tries = [words] if words else (12, 20, 32, 48, 64)
    hits = []
    for n in tries:
        hits = search(tgt, pattern(ref, vaddr, n))
        if len(hits) <= 1:
            return hits, n
    return hits, tries[-1]


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    ref, tgt = ee.Image(sys.argv[1]), ee.Image(sys.argv[2])
    if sys.argv[3] == '--table':
        for item in sys.argv[4:]:
            name, addr = item.split('=')
            hits, n = find(ref, tgt, int(addr, 16))
            print('%-24s 0x%08x -> %s  (%d words)' % (
                name, int(addr, 16),
                ', '.join('0x%08x' % h for h in hits) or 'NOT FOUND', n))
        return
    words = int(sys.argv[4]) if len(sys.argv) > 4 else None
    hits, n = find(ref, tgt, int(sys.argv[3], 16), words)
    print('%d match(es) on %d words: %s' % (
        len(hits), n, ', '.join('0x%08x' % h for h in hits)))


if __name__ == '__main__':
    main()
