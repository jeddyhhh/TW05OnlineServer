# TW05 voice chat (2026-09-29, static reading; not yet heard live)

## When and where

- Voice runs **during an online match**, not in the lobby.
- At match load, 0x001AEBE8 calls `Voip_Init` (0x001BE158), which:
  - requires broadband;
  - unloads the memory-card driver and loads `LGAUD.IRX` + `VOIPF.IRX` from `MODULES/`.
- It then calls `Voip_Connect` (0x001BE820).
- `Voip_Connect` dials the opponent **peer to peer on UDP port 6000** (0x1770, used as both local and remote port): `GOLFVOIP: Connecting to: %s:%d:%d`.
  - It uses the demangler's answer if it has one (0x0034A110), otherwise the opponent's address from the match.
  - `***** STARTING DEMANGLE FOR VOIP *****` shows it asks `demangler.ea.com` first, as the match does.
- **No voice server.** The lobby's only part is the opponent address it already hands out in `+ses`.
- In a room the game sends `auxi TEXT=H=<headset>\nL=<legend>` (0x001C2FE8, "setting user's auxi info. headset=%d, legend=%d"). The fork just acknowledges it; it may be meant to go to others (perhaps `+usr` `X`) to show headset icons. Untested.

## Hardware

- **Logitech USB Headset.**
  - `LGAUD.IRX` is Logitech's `lgaud` 1.09.006 (Dec 2003), and needs USBD ≥ 2.4.3.0.
  - `VOIPF.IRX` is "EA VoIP" (Jul 9 2004), built on it.
- PCSX2 emulates it: Settings → Controllers → USB Port 1 → **Logitech USB Headset**, then pick the PC's mic and output.

## The audio format (from VOIPF.IRX's code)

**Capture:** the driver scans the headset's formats for **mono (1 channel), 16-bit, with 8000 Hz in its supported range** (0x2174–0x2210: `channels == 1`, `bits == 16`, `min ≤ 8000 ≤ max`). So it's **8 kHz, 16-bit mono**.

**Codec: 3-bit ADPCM, EA's own IMA-style variant.** Encoder at 0x1C48, decoder at 0x1E4C.

- **Sample scaling:** each 16-bit sample is divided by 4 to 14 bits, and the predictor is clamped to ±8191.
- **The code:** the difference from the prediction is coded in 3 bits: sign (bit 2) and two magnitude bits against the current step size, which is rebuilt on the decoder side.
- **Adaptation:** the step index moves by **{−2, −1, +2, +5}** per 2-bit magnitude (table at 0x35F0). Standard 3-bit IMA/DVI4 uses {−1, −1, +1, +2}, so this is not a stock IMA stream. The step table itself (0x4214, `.bss`) is built at start-up, not stored.
- **Packing:** **8 samples into 3 bytes**, LSB-first 3-bit groups.
- **Bitrate: 8,000 × 3 = 24 kbit/s** of audio payload, one way, before packet overhead.

**Not yet known:**

- samples per packet (frame length) and the packet header;
- whether there's silence suppression;
- how the EE side (DirtySock VoIP, around 0x339670) frames it on UDP 6000.

A live capture of port 6000 during a match with headsets would answer all three.
