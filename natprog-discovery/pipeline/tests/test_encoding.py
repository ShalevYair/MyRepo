import codecs
import unittest

import _pathsetup  # noqa: F401
from natlib import encoding


class TestTables(unittest.TestCase):
    def test_cp037_matches_python_stdlib_byte_for_byte(self):
        expected = bytes(range(256)).decode("cp037")
        for b in range(256):
            self.assertEqual(encoding.decode("cp037", bytes((b,))), expected[b])

    def test_cp862_matches_python_stdlib_byte_for_byte(self):
        expected = bytes(range(256)).decode("cp862")
        for b in range(256):
            self.assertEqual(encoding.decode("cp862", bytes((b,))), expected[b])

    def test_cp424_defined_bytes_match_python_stdlib(self):
        for b in range(256):
            try:
                expected = bytes((b,)).decode("cp424")
            except UnicodeDecodeError:
                continue
            self.assertEqual(encoding.decode("cp424", bytes((b,))), expected)

    def test_cp424_undefined_bytes_map_to_replacement_char(self):
        undefined = []
        for b in range(256):
            try:
                bytes((b,)).decode("cp424")
            except UnicodeDecodeError:
                undefined.append(b)
        self.assertEqual(len(undefined), 38, "expected count of undefined CP424 bytes changed")
        for b in undefined:
            self.assertEqual(encoding.decode("cp424", bytes((b,))), "�")

    def test_cp424_hebrew_range_present(self):
        # README.md: byte range 0x60-0x7A carries the 27-char Hebrew alphabet under CP424.
        hebrew_chars = {encoding.decode("cp424", bytes((b,))) for b in encoding.CP424_HEBREW_BYTES}
        self.assertIn("א", hebrew_chars)
        self.assertIn("ת", hebrew_chars)
        self.assertGreaterEqual(len(encoding.CP424_HEBREW_BYTES), 27)

    def test_latin1_is_true_identity_mapping(self):
        # app.js had to hand-build this because JS TextDecoder('latin1') is
        # actually windows-1252. Python's 'latin-1' codec has no such trap.
        for b in (0x00, 0x20, 0x7F, 0x80, 0x9F, 0xA0, 0xFF):
            self.assertEqual(ord(encoding.decode("latin1", bytes((b,)))), b)

    def test_ascii_compatible_encodings_decode_ascii_range_identically(self):
        # EBCDIC (cp037, cp424) is deliberately excluded: it is not
        # byte-compatible with ASCII in this range at all (that's the
        # entire reason a codepage sniff is needed) -- e.g. byte 0x40 is
        # EBCDIC space, not '@'. Only genuinely ASCII-compatible codepages
        # belong in this assertion.
        ascii_sample = bytes(range(0x20, 0x7F))
        for enc in ("utf-8", "windows-1255", "iso-8859-8", "latin1", "cp862"):
            decoded = encoding.decode(enc, ascii_sample)
            self.assertEqual(decoded, ascii_sample.decode("ascii"), enc)

    def test_ebcdic_encodings_do_not_share_the_ascii_layout(self):
        # The flip side of the above: EBCDIC byte 0x40 (not 0x20) is space,
        # and byte 0x20-0x3F carries control characters, not printable
        # ASCII -- this is what sniff_encoding's ebcdic_space heuristic
        # actually detects.
        self.assertEqual(encoding.decode("cp037", b"\x40"), " ")
        self.assertEqual(encoding.decode("cp424", b"\x40"), " ")
        self.assertNotEqual(encoding.decode("cp037", bytes(range(0x20, 0x7F))),
                             bytes(range(0x20, 0x7F)).decode("ascii"))


class TestSniffEncoding(unittest.TestCase):
    def test_empty_sample(self):
        result = encoding.sniff_encoding(b"")
        self.assertEqual(result["sampled_bytes"], 0)

    def test_valid_utf8_text_guessed_as_utf8(self):
        sample = ("*H**NATPROG " * 200).encode("utf-8")
        result = encoding.sniff_encoding(sample)
        self.assertEqual(result["guess"], "utf-8")
        self.assertTrue(result["utf8_valid"])

    def test_ebcdic_space_heavy_sample_guessed_as_cp037(self):
        # 0x40 = EBCDIC space. A record padded almost entirely with 0x40,
        # with enough high bytes to clear the looks_ebcdic bar, and no
        # CP424 Hebrew-range bytes, should land on CP037 not CP424.
        sample = bytes([0x40] * 700 + [0xC1, 0xC2, 0xC3] * 150)
        result = encoding.sniff_encoding(sample)
        self.assertEqual(result["guess"], "cp037")

    def test_ebcdic_with_hebrew_bytes_guessed_as_cp424(self):
        # CP424's Hebrew block sits at 0x41-0x71 (confirmed: all 27 bytes
        # are < 0x80), so it does NOT by itself clear the looksEbcdic
        # high/n > 0.10 gate. Real EBCDIC Hebrew source has that gate
        # cleared by ordinary high-byte EBCDIC punctuation/accented
        # Latin elsewhere in the record; the sample must supply that
        # separately from the Hebrew evidence.
        hebrew_bytes = encoding.CP424_HEBREW_BYTES[:5]
        sample = bytes([0x40] * 700 + [0xC1, 0xC2] * 150 + hebrew_bytes * 60)
        result = encoding.sniff_encoding(sample)
        self.assertEqual(result["guess"], "cp424")

    def test_line_ending_detection_crlf(self):
        sample = b"AAAA\r\nBBBB\r\nCCCC\r\n"
        result = encoding.sniff_encoding(sample)
        self.assertEqual(result["line_ending"], "CRLF")

    def test_pre_existing_replacement_bytes_detected(self):
        sample = "clean text � more text".encode("utf-8")
        result = encoding.sniff_encoding(sample)
        self.assertEqual(result["replacement_seq_in_sample"], 1)
        self.assertIn("U+FFFD", result["why"])


if __name__ == "__main__":
    unittest.main()
