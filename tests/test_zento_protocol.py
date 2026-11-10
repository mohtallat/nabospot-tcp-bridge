import unittest

from zento_protocol import (
    ZentoFrameBuffer,
    ZentoProtocolError,
    build_frame,
    decode_status,
    parse_frame,
)


SAMPLES = {
    "open": (
        "AA 55 16 00 00 0A 04 0B 00 00 00 00 "
        "12 00 00 00 77 00 C3 0A 00 00 85 FF"
    ),
    "opening": (
        "AA 55 16 00 00 0A 03 12 00 00 00 00 "
        "12 00 00 00 77 00 C3 0B 03 00 8F FF"
    ),
    "closing": (
        "AA 55 16 00 00 0A 05 0D 00 00 00 00 "
        "02 00 00 00 76 25 C4 0B 3A 00 D8 FF"
    ),
    "closed": (
        "AA 55 16 00 00 0A 06 0E 00 00 00 00 "
        "00 00 00 00 77 00 C3 0B 5E 00 D7 FF"
    ),
    "stopped": (
        "AA 55 16 00 00 0A 07 1F 00 00 00 00 "
        "00 00 00 00 76 12 C4 0C 26 00 C4 FF"
    ),
}


class ZentoProtocolTests(unittest.TestCase):

    def test_documented_commands(self):
        expected = {
            "open": "AA 55 07 00 00 02 00 09 FF",
            "close": "AA 55 07 00 00 03 00 0A FF",
            "stop": "AA 55 07 00 00 04 00 0B FF",
            "status": "AA 55 07 00 00 0A 00 11 FF",
        }

        for name, hex_frame in expected.items():
            with self.subTest(command=name):
                self.assertEqual(
                    build_frame(name),
                    bytes.fromhex(hex_frame),
                )

    def test_verified_reversed_commands(self):
        self.assertEqual(
            build_frame(
                "open", reversed_direction=True
            ),
            bytes.fromhex(
                "AA 55 07 00 00 03 00 0A FF"
            ),
        )

        self.assertEqual(
            build_frame(
                "close", reversed_direction=True
            ),
            bytes.fromhex(
                "AA 55 07 00 00 02 00 09 FF"
            ),
        )

    def test_all_physical_status_samples(self):
        for expected_state, hex_frame in SAMPLES.items():
            with self.subTest(state=expected_state):
                frame = parse_frame(
                    bytes.fromhex(hex_frame)
                )
                self.assertEqual(
                    decode_status(
                        frame,
                        reversed_direction=True,
                    ),
                    expected_state,
                )

    def test_invalid_checksum(self):
        raw = bytearray(
            bytes.fromhex(SAMPLES["open"])
        )
        raw[-2] ^= 0x01

        with self.assertRaises(ZentoProtocolError):
            parse_frame(raw)

    def test_invalid_length(self):
        raw = bytearray(
            bytes.fromhex(SAMPLES["closed"])
        )
        raw[2] = 0x15

        with self.assertRaises(ZentoProtocolError):
            parse_frame(raw)

    def test_fragmented_tcp_frame(self):
        raw = bytes.fromhex(SAMPLES["opening"])
        parser = ZentoFrameBuffer()

        self.assertEqual(parser.feed(raw[:5]), [])
        self.assertEqual(parser.feed(raw[5:12]), [])

        frames = parser.feed(raw[12:])

        self.assertEqual(len(frames), 1)
        self.assertEqual(
            decode_status(
                frames[0],
                reversed_direction=True,
            ),
            "opening",
        )

    def test_leading_noise(self):
        raw = bytes.fromhex(SAMPLES["closed"])
        parser = ZentoFrameBuffer()

        frames = parser.feed(b"\x00\x01" + raw)

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, raw)

    def test_multiple_frames(self):
        parser = ZentoFrameBuffer()

        raw = (
            bytes.fromhex(SAMPLES["open"])
            + bytes.fromhex(SAMPLES["closed"])
        )

        frames = parser.feed(raw)

        self.assertEqual(len(frames), 2)

    def test_corrupted_frame_recovery(self):
        bad = bytearray(
            bytes.fromhex(SAMPLES["open"])
        )
        bad[-2] ^= 0x01

        good = bytes.fromhex(SAMPLES["closed"])
        parser = ZentoFrameBuffer()

        frames = parser.feed(bytes(bad) + good)

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].raw, good)

    def test_invalid_address(self):
        with self.assertRaises(ValueError):
            build_frame("status", address=256)

    def test_unknown_status(self):
        raw = bytearray(
            bytes.fromhex(SAMPLES["stopped"])
        )
        raw[6] = 0x08
        raw[-2] = sum(raw[2:-2]) & 0xFF

        frame = parse_frame(raw)

        self.assertEqual(
            decode_status(
                frame,
                reversed_direction=True,
            ),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
