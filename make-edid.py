#!/usr/bin/python3
"""Generate the EDID for the phantom iPad Air 13" display (2732x2048, 4:3).

Base block: 2732x2048@60 (preferred), 2732x2048@57, 2048x1536@60, name.
CTA-861 block: HDMI VSDB + HDMI Forum VSDB (so i915 treats the sink as HDMI
2.0 instead of single-link DVI capped at 165 MHz), plus 2732x2048@50.
@60 needs ~355 MHz (> 340 MHz, scrambling); @57 and @50 stay under 340 MHz.
"""
import argparse
import math
from pathlib import Path

NAME = "iPad Virtual"
# 264 ppi panel: 2732 px = 263 mm, 2048 px = 197 mm. Drives GNOME's default scale.
WIDTH_MM, HEIGHT_MM = 263, 197


def cvt_rb2(h, v, refresh):
    """VESA CVT reduced blanking v2 timing."""
    h_fp, h_sync, h_bp = 8, 32, 40
    v_sync, v_bp, v_fp_min, min_vblank_us = 8, 6, 1, 460
    h_period_us = (1e6 / refresh - min_vblank_us) / v
    vbi = max(int(min_vblank_us / h_period_us) + 1, v_fp_min + v_sync + v_bp)
    h_total, v_total = h + h_fp + h_sync + h_bp, v + vbi
    clock_10khz = math.floor(h_total * v_total * refresh / 10_000)
    return dict(clock_10khz=clock_10khz, h=h, h_blank=h_total - h, h_fp=h_fp, h_sync=h_sync,
                v=v, v_blank=vbi, v_fp=vbi - v_sync - v_bp, v_sync=v_sync,
                refresh=clock_10khz * 10_000 / (h_total * v_total))


def dtd(t):
    hi = lambda value, shift: (value >> 8) << shift
    b = bytearray(18)
    b[0:2] = t["clock_10khz"].to_bytes(2, "little")
    b[2], b[3] = t["h"] & 0xFF, t["h_blank"] & 0xFF
    b[4] = hi(t["h"], 4) | hi(t["h_blank"], 0)
    b[5], b[6] = t["v"] & 0xFF, t["v_blank"] & 0xFF
    b[7] = hi(t["v"], 4) | hi(t["v_blank"], 0)
    b[8], b[9] = t["h_fp"] & 0xFF, t["h_sync"] & 0xFF
    b[10] = ((t["v_fp"] & 0xF) << 4) | (t["v_sync"] & 0xF)
    b[11] = (hi(t["h_fp"], 6) | hi(t["h_sync"], 4)
             | ((t["v_fp"] >> 4) << 2) | (t["v_sync"] >> 4))
    b[12], b[13] = WIDTH_MM & 0xFF, HEIGHT_MM & 0xFF
    b[14] = ((WIDTH_MM >> 8) << 4) | (HEIGHT_MM >> 8)
    b[17] = 0x1A  # digital separate sync, hsync +, vsync - (reduced blanking)
    return bytes(b)


def name_descriptor(name):
    text = name.encode("ascii")[:13]
    text += b"\n" + b" " * (12 - len(text)) if len(text) < 13 else b""
    return bytes([0, 0, 0, 0xFC, 0]) + text


def checksum(block):
    block[127] = (-sum(block[:127])) & 0xFF


def base_block(timings):
    b = bytearray(128)
    b[0:8] = bytes([0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0])
    vendor = "SIV"  # "Sunshine iPad Virtual"; not a registered PNP ID
    b[8:10] = sum((ord(c) - 64) << s for c, s in zip(vendor, (10, 5, 0))).to_bytes(2, "big")
    b[10:12] = (0x2732).to_bytes(2, "little")
    b[12:16] = (1).to_bytes(4, "little")
    b[16], b[17], b[18], b[19] = 41, 2026 - 1990, 1, 4
    b[20] = 0xA2  # digital, 8 bpc, HDMI-a
    b[21], b[22] = round(WIDTH_MM / 10), round(HEIGHT_MM / 10)
    b[23] = 120   # gamma 2.2
    b[24] = 0x06  # RGB 4:4:4, sRGB default, preferred timing is native
    b[25:35] = bytes.fromhex("EE91A3544C99260F5054")  # sRGB chromaticity
    b[38:54] = b"\x01" * 16  # no standard timings
    descriptors = [dtd(t) for t in timings] + [name_descriptor(NAME)]
    for i, d in enumerate(descriptors):
        b[54 + 18 * i: 72 + 18 * i] = d
    b[126] = 1
    checksum(b)
    return b


def cta_block(timings):
    b = bytearray(128)
    hdmi_vsdb = bytes([0x67, 0x03, 0x0C, 0x00, 0x10, 0x00, 0x00, 340 // 5])
    hf_vsdb = bytes([0x67, 0xD8, 0x5D, 0xC4, 0x01, 600 // 5, 0x80, 0x00])  # SCDC present
    data = hdmi_vsdb + hf_vsdb
    b[0], b[1], b[2], b[3] = 0x02, 0x03, 4 + len(data), 0x00
    b[4:4 + len(data)] = data
    for i, t in enumerate(timings):
        start = 4 + len(data) + 18 * i
        b[start:start + 18] = dtd(t)
    checksum(b)
    return b


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    base = [cvt_rb2(2732, 2048, 60), cvt_rb2(2732, 2048, 57), cvt_rb2(2048, 1536, 60)]
    extra = [cvt_rb2(2732, 2048, 50)]
    edid = bytes(base_block(base)) + bytes(cta_block(extra))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(edid)
    for t in base + extra:
        print(f"{t['h']}x{t['v']} @ {t['refresh']:.3f} Hz, pixel clock {t['clock_10khz'] / 100:.2f} MHz")
    print(f"wrote {len(edid)} bytes to {args.output}")


if __name__ == "__main__":
    main()
