"""The DS1054Z screenshot on old and new firmware.

00.04.04.SP1 answers `:DISPlay:DATA? ON,0,PNG`; 00.04.03 refuses the
parameters and hangs the USB session until a device clear, but answers the
bare `:DISPlay:DATA?` with a BMP. The profile picks the form from *IDN?
before asking, and re-encodes a BMP as PNG. This checks the choice against a
fake session for each firmware, and the re-encoding pixel for pixel.

    python tests/test_rigol_screenshot.py
"""
import os
import struct
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scope_profiles  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAILS.append(label)


def make_bmp(rgb, bpp=24, top_down=False):
    """A BMP of `rgb` (h, w, 3 uint8), written the way the scope writes one."""
    h, w, _ = rgb.shape
    step = bpp // 8
    stride = (w * step + 3) & ~3
    rows = np.zeros((h, stride), np.uint8)
    px = np.zeros((h, w, step), np.uint8)
    px[:, :, :3] = rgb[:, :, ::-1]                     # RGB -> BGR
    if step == 4:
        px[:, :, 3] = 255
    rows[:, :w * step] = px.reshape(h, w * step)
    if not top_down:
        rows = rows[::-1]
    data = rows.tobytes()
    header = struct.pack("<2sIHHI", b"BM", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, bpp,
                       0, len(data), 2835, 2835, 0, 0)
    return header + info + data


def decode_png(png):
    """Just enough of a PNG reader for what bmp_to_png writes: 8-bit RGB,
    filter 0 on every row, one IDAT."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", None, None
    chunks = []
    while pos < len(png):
        n = struct.unpack_from(">I", png, pos)[0]
        kind = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + n]
        crc = struct.unpack_from(">I", png, pos + 8 + n)[0]
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, kind
        chunks.append(kind)
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack_from(">IIBB", data)
            assert (depth, ctype) == (8, 2)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, w * 3 + 1)
    assert not raw[:, 0].any(), "every row should use filter 0"
    return raw[:, 1:].reshape(h, w, 3), chunks


class FakeInst:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def query_binary_values(self, cmd, datatype="B", container=bytearray):
        self.asked.append(cmd)
        if cmd not in self.answers:
            raise TimeoutError(f"{cmd!r}: VI_ERROR_TMO")    # what 00.04.03 does
        return container(self.answers[cmd])


class FakeScope:
    def __init__(self, idn, answers):
        self.idn = idn
        self.inst = FakeInst(answers)


def main():
    rng = np.random.default_rng(1054)

    print("BMP to PNG")
    # 801 wide so a 24-bit row needs padding; the scope's own is 800x480.
    for (h, w), bpp, top in (((6, 801), 24, False), ((480, 800), 24, False),
                             ((5, 7), 32, False), ((5, 7), 24, True)):
        rgb = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        png = scope_profiles.bmp_to_png(make_bmp(rgb, bpp, top))
        got, chunks = decode_png(png)
        check(f"{w}x{h} {bpp}-bit {'top-down' if top else 'bottom-up'}: "
              "every pixel survives", np.array_equal(got, rgb))
    check("IHDR, IDAT, IEND in order", chunks == [b"IHDR", b"IDAT", b"IEND"])
    try:
        scope_profiles.bmp_to_png(b"\x89PNG not a bmp")
        check("a non-BMP is refused", False)
    except ValueError:
        check("a non-BMP is refused", True)

    print("\nfirmware from *IDN?")
    fv = scope_profiles.firmware_version
    for idn, want in (("RIGOL TECHNOLOGIES,DS1054Z,DS1ZA184752794,00.04.04.SP1", (0, 4, 4)),
                      ("RIGOL TECHNOLOGIES,DS1054Z,DS1ZA171408768,00.04.03", (0, 4, 3)),
                      ("RIGOL TECHNOLOGIES,DS1054Z,DS1ZA1,00.04.05.SP2", (0, 4, 5)),
                      ("RIGOL TECHNOLOGIES,DS1054Z", None),
                      ("", None)):
        check(f"{idn or '(empty)'!s} -> {want}", fv(idn) == want, str(fv(idn)))

    print("\nthe form asked for")
    prof = scope_profiles.PROFILES["ds1054z"]
    rgb = rng.integers(0, 256, (480, 800, 3), dtype=np.uint8)
    bmp = make_bmp(rgb)
    native_png = b"\x89PNG\r\n\x1a\nnative"

    new = FakeScope("RIGOL TECHNOLOGIES,DS1054Z,DS1ZA184752794,00.04.04.SP1",
                    {":DISPlay:DATA? ON,0,PNG": native_png, ":DISPlay:DATA?": bmp})
    img = prof.screenshot(new)
    check("00.04.04.SP1: the scope's own PNG, one query",
          bytes(img) == native_png and new.inst.asked == [":DISPlay:DATA? ON,0,PNG"],
          str(new.inst.asked))

    old = FakeScope("RIGOL TECHNOLOGIES,DS1054Z,DS1ZA171408768,00.04.03",
                    {":DISPlay:DATA?": bmp})
    img = prof.screenshot(old)
    check("00.04.03: never sends the parameters it hangs on",
          old.inst.asked == [":DISPlay:DATA?"], str(old.inst.asked))
    check("00.04.03: still a PNG, of exactly the screen",
          bytes(img[:4]) == b"\x89PNG" and np.array_equal(decode_png(bytes(img))[0], rgb))
    check("returned as a bytearray, like the native path", isinstance(img, bytearray))

    unknown = FakeScope("", {":DISPlay:DATA?": bmp})
    prof.screenshot(unknown)
    check("an unreadable version takes the form every firmware answers",
          unknown.inst.asked == [":DISPlay:DATA?"])

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("Rigol screenshot OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
