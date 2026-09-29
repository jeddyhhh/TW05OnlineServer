"""Build TW05's LobbyAPI request map: verb, tags sent, callback, per function.

The TW05 port of the TW04 tool.  Every request still goes through one routine
-- LobbyApiRequest(pApi, u4CC, pTagBuf, pCallback), at 0x00326EC0 in
SLUS_210.02 -- but TW05 builds the tag buffer differently: instead of
TagFieldSetString/SetNumber calls with a key and a value, it formats whole
lines from strings like "NAME=%d", or passes bare keys ("PERS") to its own
setters.  So the tags a function SENDS are the "KEY=..." strings and bare
upper-case keys it references, `value` lines are short lower-case literals
(CMD values such as 'mg5ri'), and the tags a callback READS are the bare
upper-case keys IT references.  A key the code builds at run time is missed.

usage: requests_map.py <elf> [LobbyApiRequest vaddr]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ee                                               # noqa: E402
from xref import load                                   # noqa: E402
from tagscan import track, ARGS                         # noqa: E402
import names as namemod                                 # noqa: E402

REQUEST = 0x00326EC0          # LobbyApiRequest in SLUS_210.02 (from Lobby_GetNews)
SENDS = re.compile(r'^[A-Z][A-Z0-9_]{1,15}=')
READS = re.compile(r'^[A-Z][A-Z0-9_]{1,15}$')
VALUE = re.compile(r'^[a-z0-9@]{4,6}$')         # CMD values such as 'mg5ri'


def fourcc(v):
    if v is None:
        return '????'
    b = v.to_bytes(4, 'big')
    return b.decode('ascii') if all(0x20 <= c < 0x7f for c in b) else '0x%08x' % v


def strings_by_function(img, consts):
    """{function start: [string, ...]} for every string a function references."""
    out = {}
    for target, sites in consts.items():
        if not img.valid(target):
            continue
        s = img.cstr(target, 96)
        if not s or not all(0x20 <= ord(c) < 0x7f for c in s):
            continue
        for site in sites:
            fs = ee.func_start(img, site)
            if fs is not None:
                out.setdefault(fs, []).append((site, s))
    return {fs: [s for _site, s in sorted(v)] for fs, v in out.items()}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    request = int(sys.argv[2], 16) if len(sys.argv) > 2 else REQUEST
    img, consts, calls = load(path)
    fname = namemod.build(img, consts)
    strs = strings_by_function(img, consts)

    rows = []
    for site in calls.get(request, []):
        fs = ee.func_start(img, site)
        reg = track(img, fs, site) if fs is not None else {}
        cb = reg.get(ARGS[3])
        rows.append((fname.get(fs, 'sub_%08x' % (fs or 0)), fs, site,
                     fourcc(reg.get(ARGS[1])), cb))
    rows.sort()

    print('# LobbyAPI requests in %s' % os.path.basename(path))
    print('# LobbyApiRequest = 0x%08x, %d call sites\n' % (request, len(rows)))
    for name, fs, site, cc, cb in rows:
        cbname = fname.get(cb, '0x%08x' % cb) if cb else '-'
        print("%-38s 0x%08x  verb '%s'  -> %s" % (name, fs or 0, cc, cbname))
        for s in dict.fromkeys(strs.get(fs, [])):
            if SENDS.match(s) or READS.match(s):
                print('        send  %s' % s)
            elif VALUE.match(s):
                print("        value '%s'" % s)
        if cb:
            got = [s for s in dict.fromkeys(strs.get(cb, [])) if READS.match(s)]
            if got:
                print('        read  %s' % ', '.join(got))
        print()


if __name__ == '__main__':
    main()
