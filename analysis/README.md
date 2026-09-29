# Analysis helpers

The small scripts the [research notes](../docs/README.md) were worked out
with: a disassembler that understands this compiler's habits, cross-references,
constant recovery at call sites, and function names recovered from the game's
own debug strings. None of them are needed to run the server.

Most of them came over from the
[TW04 server](https://github.com/jeddyhhh/TW04OnlineServer), which shares
EA's lobby library with TW05; `sigmatch.py` and `field_getters.py` are new here.

## Setup

Unlike the server, these need two libraries:

```bash
pip install -r analysis/requirements.txt     # capstone and pyelftools
```

They work on **`SLUS_210.02`**, the game's main executable. It's a file in
the root of the disc, so any ISO tool will extract it from your own image of
Tiger Woods PGA Tour 2005 (USA, `SLUS-21002`), for example with 7-Zip:

```bash
7z e "Tiger Woods PGA Tour 2005.iso" SLUS_210.02
```

Nothing from the game is included in this repository.

## Addresses

The ELF is one `PT_LOAD` at `0x00100000`, file offset `0x1000`, so

```
virtual address = file offset + 0xFF000
```

Every address in the notes, and every argument these tools take, is a
**virtual address** in hex, except `strings.py`, which prints file offsets
(like `strings -t x`). `elfinfo.py` prints the mapping for any ELF.

## The tools

Run them from anywhere, e.g. `python analysis/xref.py SLUS_210.02 ps2tw05.ea.com`.

| Script | What it does | Example |
|---|---|---|
| `strings.py <file> [minlen]` | printable strings, tagged with their file offset | `strings.py SLUS_210.02 8` |
| `elfinfo.py <elf>` | segment and section layout, and the address map | `elfinfo.py SLUS_210.02` |
| `xref.py <elf> <addr\|string> ...` | who references an address, or a string by its text | `xref.py SLUS_210.02 demangler.ea.com` |
| `disfn.py <elf> <addr> [n]` | disassembly, with string and constant operands annotated | `disfn.py SLUS_210.02 0x001C3350 40` |
| `tagscan.py <elf> <callee> [argc] [--sort]` | the constant arguments at every call site of a function | `tagscan.py SLUS_210.02 0x00326EC0` |
| `names.py <elf>` | function names recovered from the debug labels the code passes around | `names.py SLUS_210.02` |
| `requests_map.py <elf>` | every LobbyAPI request: its verb, the tags it sends, its callback | `requests_map.py SLUS_210.02` |
| `sigmatch.py <ref elf> <elf> <addr>` | find a TW04 function in TW05 by its masked machine code | `sigmatch.py SLUS_207.57 SLUS_210.02 <TW04 address>` |
| `field_getters.py <elf> <getter>` | what each caller of a "get entry" routine reads from the entry | `field_getters.py SLUS_210.02 <getter> --base 0x20` |

`requests_map.py` is what produced
[`docs/tw05-lobby-requests.txt`](../docs/tw05-lobby-requests.txt).
`field_getters.py` is how the tournament calendar's layout was mapped: the
game reads each field of an event through its own small accessor.

Two modules are shared by the rest rather than run:

- `ee.py` loads the ELF and holds the address map, the constant and `jal`
  scanners, and the function-start finder.
- `mips.py` decodes the 64-bit forms capstone gets wrong on the PS2's R5900
  (below).

## Notes

- **The first run on an ELF takes a while.** `xref.py` builds an index of
  every constant and call in the image and keeps it in `analysis/_cache/`, so
  later runs are instant. Set `TW_CACHE` to keep it somewhere else, and delete
  it if you change `ee.py`.
- **capstone only knows MIPS32.** The PS2's R5900 has 64-bit instructions the
  compiler uses all the time, and capstone shows those as `.byte` lines or
  nonsense. `disfn.py` decodes them itself through `mips.py`, and `tagscan.py`
  does its own constant propagation for the same reason.
- **TW05 was built by a different compiler from TW04**, so the game's own code
  never matches byte for byte between the two, and only EA's shared library
  code does, which is what `sigmatch.py` is for. Port by strings and names;
  `names.py` recovers 77 of them.
