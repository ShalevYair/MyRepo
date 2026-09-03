import hashlib
import json
import pathlib
import tempfile
import unittest

import _pathsetup  # noqa: F401
import natunload_split as split
from natlib import natprofile as prof


def _line(total_len: int, *puts: tuple[int, str]) -> str:
    """Build a fixed-width record: total_len chars, space-padded, with each
    (offset, text) in `puts` written at that offset. Mirrors the manual
    line-building in tests/test_natprofile.py. Every real raw-unload record
    length is a multiple of natprofile.RECORD_PAD (12) -- asserted here so a
    broken fixture fails loudly instead of silently testing the wrong thing."""
    buf = [" "] * total_len
    for offset, text in puts:
        buf[offset : offset + len(text)] = list(text)
    s = "".join(buf)
    assert len(s) % prof.RECORD_PAD == 0, f"fixture length {len(s)} is not a multiple of 12"
    return s


def header_line(prod="NAT", version="8207", ts="202608131404305", os_="MVS/ESA") -> str:
    return _line(36, (0, "*H**"), (4, "E"), (5, prod), (8, version), (12, ts), (27, os_))


def c_line(library: str, name: str, type_letter: str) -> str:
    return _line(84, (0, "*C**"), (36, library), (44, name), (76, type_letter))


def d01_line(library: str, name: str, type_letter: str, version="0001",
             user1="", user2="", user3="") -> str:
    return _line(84, (0, "*D01"), (4, "NAT"), (7, version), (11, type_letter), (13, library),
                 (21, name), (53, user1), (61, user2), (69, user3))


def d02_line(saved_ts: str, cataloged_ts: str, size: str) -> str:
    return _line(60, (0, "*D02"), (16, saved_ts), (31, cataloged_ts), (46, size))


def d03_line(os_="MVS/ESA", tp="CICS") -> str:
    return _line(36, (0, "*D03"), (4, os_), (12, tp))


def d04_line(codepage="IBM01140") -> str:
    return _line(36, (0, "*D04"), (21, codepage))


def s_line(payload: str, tag="*S**") -> str:
    total = 12
    while 4 + len(payload) > total:
        total += 12
    return _line(total, (0, tag), (4, payload))


def one_object(library="RC", name="PROG1", type_letter="F", extra_source=None) -> list[str]:
    lines = [
        c_line(library, name, type_letter),
        d01_line(library, name, type_letter, user1="USERA"),
        d02_line("201503081500150", "201503081500150", "0000000123"),
        d03_line(),
        d04_line(),
    ]
    for payload in (extra_source or ["DEFINE DATA", "END-DEFINE"]):
        lines.append(s_line(payload))
    return lines


class TestPureFunctions(unittest.TestCase):
    def test_detect_framing_lf(self):
        self.assertEqual(split.detect_framing("a\nb\nc"), "\n")

    def test_detect_framing_nel(self):
        self.assertEqual(split.detect_framing("a\x85b\x85c"), "\x85")

    def test_detect_framing_defaults_to_lf_when_neither_present(self):
        self.assertEqual(split.detect_framing("abc"), "\n")

    def test_sanitize_component_replaces_unsafe_chars(self):
        self.assertEqual(split.sanitize_component('A/B:C*D', "X"), "A_B_C_D")

    def test_sanitize_component_empty_uses_placeholder(self):
        self.assertEqual(split.sanitize_component("   ", "PLACEHOLDER"), "PLACEHOLDER")

    def test_sanitize_component_strips_trailing_dot_and_space(self):
        self.assertEqual(split.sanitize_component("NAME. ", "X"), "NAME")


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def _run(self, lines, **kw):
        unload_file = self.tmp_path / "unload.txt"
        unload_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out_dir = self.tmp_path / "out"
        stats = split.run_split(
            unload_path=unload_file, out_dir=out_dir, encoding_opt="utf-8",
            chunk_bytes=kw.pop("chunk_bytes", 8 * 1024 * 1024),
            sniff_bytes=4 * 1024 * 1024,
            limit_bytes=kw.pop("limit_bytes", None),
        )
        rows = [json.loads(l) for l in (out_dir / "objects.jsonl").read_text(encoding="utf-8").splitlines()] \
            if (out_dir / "objects.jsonl").exists() else []
        return stats, out_dir, rows

    def test_single_object_round_trip(self):
        lines = [header_line()] + one_object("RC", "PROG1", "F", extra_source=["DEFINE DATA", "", "END-DEFINE"])
        stats, out_dir, rows = self._run(lines)

        self.assertEqual(stats.objects_seen, 1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["object_id"], "RC/PROG1")
        self.assertEqual(row["library"], "RC")
        self.assertEqual(row["name"], "PROG1")
        self.assertEqual(row["type"], "F")
        self.assertEqual(row["type_meaning"], "Program")
        self.assertEqual(row["kind"], "exec")
        self.assertEqual(row["type_confidence"], "confirmed")
        self.assertEqual(row["nat_version"], "0001")
        self.assertEqual(row["saved"], "2015-03-08T15:00:15")
        self.assertEqual(row["cataloged"], "2015-03-08T15:00:15")
        self.assertEqual(row["size"], 123)
        self.assertEqual(row["lines"], 3)  # DEFINE DATA / '' / END-DEFINE
        self.assertEqual(row["chars"], len("DEFINE DATA") + 0 + len("END-DEFINE"))
        self.assertEqual(row["os"], "MVS/ESA")
        self.assertEqual(row["tp"], "CICS")
        self.assertEqual(row["codepage"], "IBM01140")
        self.assertEqual(row["users"], ["USERA"])  # blanks filtered out
        self.assertEqual(row["source_path"], "source/RC/PROG1.nat")
        self.assertFalse(row["truncated"])

        # sha256_raw must match the bytes actually on disk -- not just the
        # in-memory string -- otherwise a consumer reading source_path
        # directly would silently get content that doesn't match the hash.
        on_disk = (out_dir / row["source_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(on_disk).hexdigest(), row["sha256_raw"])
        self.assertEqual(on_disk.decode("utf-8"), "DEFINE DATA\n\nEND-DEFINE\n")

    def test_dash_s_counts_as_source_line(self):
        lines = [c_line("RC", "PROG2", "F"), d01_line("RC", "PROG2", "F"),
                 d02_line("0" * 15, "0" * 15, " " * 10),
                 d03_line(), d04_line(),
                 s_line("DEFINE DATA", tag="*S**"),
                 s_line("/*DV SOME DIRECTIVE", tag="-S**"),
                 s_line("END-DEFINE", tag="*S**")]
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.tag_counts["-S**"], 1)
        self.assertEqual(rows[0]["lines"], 3)
        content = (out_dir / rows[0]["source_path"]).read_text(encoding="utf-8")
        self.assertIn("/*DV SOME DIRECTIVE", content)

    def test_all_zero_timestamp_is_null_not_error(self):
        lines = [c_line("RC", "PROG3", "F"), d01_line("RC", "PROG3", "F"),
                 d02_line("0" * 15, "0" * 15, "0000000010"),
                 d03_line(), d04_line(), s_line("X")]
        stats, out_dir, rows = self._run(lines)
        self.assertIsNone(rows[0]["saved"])
        self.assertIsNone(rows[0]["cataloged"])
        self.assertEqual(stats.bad_saved_ts, 0)  # empty is not "bad", just absent
        self.assertEqual(rows[0]["size"], 10)

    def test_unknown_type_letter_outside_type_map(self):
        # 'Z' is not in TYPE_MAP at all -- must degrade gracefully, never crash
        # (WORKPLAN.md 1.1 risk note).
        lines = [c_line("RC", "MYSTERY", "Z"), d01_line("RC", "MYSTERY", "Z"),
                 d02_line("0" * 15, "0" * 15, " " * 10), d03_line(), d04_line(),
                 s_line("SOMETHING")]
        stats, out_dir, rows = self._run(lines)
        row = rows[0]
        self.assertIsNone(row["type_meaning"])
        self.assertIsNone(row["kind"])
        self.assertEqual(row["type_confidence"], "none")
        self.assertEqual(stats.unknown_type_letters["Z"], 1)

    def test_letter_7_is_null_meaning_not_the_word_unknown(self):
        # SCHEMAS.md section 1: type_meaning must be null for '7'/'5', even
        # though natlib.natprofile.TYPE_MAP internally labels them name="UNKNOWN".
        lines = [c_line("RC", "SEVEN", "7"), d01_line("RC", "SEVEN", "7"),
                 d02_line("0" * 15, "0" * 15, " " * 10), d03_line(), d04_line(),
                 s_line("X")]
        _, _, rows = self._run(lines)
        row = rows[0]
        self.assertIsNone(row["type_meaning"])
        self.assertEqual(row["type_confidence"], "none")

    def test_orphan_source_line_before_any_object_does_not_crash(self):
        lines = [s_line("STRAY LINE"), c_line("RC", "PROG4", "F"),
                 d01_line("RC", "PROG4", "F"), d02_line("0" * 15, "0" * 15, " " * 10),
                 d03_line(), d04_line(), s_line("REAL LINE")]
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.orphan_source_lines, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lines"], 1)

    def test_directory_record_without_object_does_not_crash(self):
        lines = [d02_line("0" * 15, "0" * 15, " " * 10), c_line("RC", "PROG5", "F"),
                 d01_line("RC", "PROG5", "F"), d02_line("0" * 15, "0" * 15, " " * 10),
                 d03_line(), d04_line(), s_line("X")]
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.directory_without_object, 1)
        self.assertEqual(len(rows), 1)

    def test_two_objects_different_libraries_are_sharded_separately(self):
        lines = one_object("RC", "PROGA", "F") + one_object("RCOLD", "PROGA", "N")
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.objects_seen, 2)
        ids = {r["object_id"] for r in rows}
        self.assertEqual(ids, {"RC/PROGA", "RCOLD/PROGA"})
        self.assertTrue((out_dir / "source" / "RC" / "PROGA.nat").exists())
        self.assertTrue((out_dir / "source" / "RCOLD" / "PROGA.nat").exists())

    def test_duplicate_library_name_key_gets_disambiguated_filename(self):
        lines = one_object("RC", "DUP1", "F", extra_source=["FIRST"]) + \
                one_object("RC", "DUP1", "F", extra_source=["SECOND"])
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.duplicate_object_keys["RC/DUP1"], 1)
        self.assertEqual(rows[0]["source_path"], "source/RC/DUP1.nat")
        self.assertEqual(rows[1]["source_path"], "source/RC/DUP1~2.nat")
        first = (out_dir / rows[0]["source_path"]).read_text(encoding="utf-8")
        second = (out_dir / rows[1]["source_path"]).read_text(encoding="utf-8")
        self.assertIn("FIRST", first)
        self.assertIn("SECOND", second)
        # each row's hash matches ITS OWN file, not the other one
        self.assertEqual(hashlib.sha256(first.encode("utf-8")).hexdigest(), rows[0]["sha256_raw"])
        self.assertEqual(hashlib.sha256(second.encode("utf-8")).hexdigest(), rows[1]["sha256_raw"])

    def test_duplicate_detection_uses_normalized_filename_not_raw_case(self):
        # Regression: two objects differing only by case resolve to the SAME
        # on-disk filename (both get upper-cased there) and must therefore be
        # treated as a collision -- keying the dedup check on the raw,
        # not-yet-uppercased (library, name) would miss this and let the
        # second object silently overwrite the first one's file.
        lines = one_object("rc", "dup2", "F", extra_source=["FIRST"]) + \
                one_object("RC", "DUP2", "F", extra_source=["SECOND"])
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(rows[0]["source_path"], "source/RC/DUP2.nat")
        self.assertEqual(rows[1]["source_path"], "source/RC/DUP2~2.nat")
        first = (out_dir / rows[0]["source_path"]).read_text(encoding="utf-8")
        second = (out_dir / rows[1]["source_path"]).read_text(encoding="utf-8")
        self.assertIn("FIRST", first)
        self.assertIn("SECOND", second)

    def test_empty_library_and_name_get_placeholder_directories(self):
        lines = [c_line("", "", "F"), d01_line("", "", "F"),
                 d02_line("0" * 15, "0" * 15, " " * 10), d03_line(), d04_line(), s_line("X")]
        stats, out_dir, rows = self._run(lines)
        self.assertEqual(stats.empty_library, 1)
        self.assertEqual(stats.empty_name, 1)
        self.assertEqual(rows[0]["object_id"], "/")
        self.assertTrue((out_dir / "source" / "_EMPTY_LIBRARY_" / "_EMPTY_NAME_.nat").exists())

    def test_crlf_line_endings_are_tolerated(self):
        lines = one_object("RC", "CRLFTEST", "F")
        unload_file = self.tmp_path / "crlf.txt"
        unload_file.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        out_dir = self.tmp_path / "out_crlf"
        stats = split.run_split(unload_path=unload_file, out_dir=out_dir, encoding_opt="utf-8",
                                 chunk_bytes=8 * 1024 * 1024, sniff_bytes=4 * 1024 * 1024, limit_bytes=None)
        rows = [json.loads(l) for l in (out_dir / "objects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(stats.objects_seen, 1)
        self.assertEqual(rows[0]["object_id"], "RC/CRLFTEST")
        # the trailing \r must not leak into the source body
        content = (out_dir / rows[0]["source_path"]).read_text(encoding="utf-8")
        self.assertNotIn("\r", content)

    def test_limit_bytes_marks_final_object_truncated(self):
        lines = one_object("RC", "FULL1", "F") + one_object("RC", "PARTIAL1", "F",
                                                              extra_source=["LINE" + str(i) for i in range(50)])
        full_text = "\n".join(lines) + "\n"
        unload_file = self.tmp_path / "big.txt"
        unload_file.write_bytes(full_text.encode("utf-8"))
        # cut partway through the second object's source lines
        second_c_pos = full_text.index(c_line("RC", "PARTIAL1", "F"))
        cut_at = second_c_pos + 200
        out_dir = self.tmp_path / "out_limit"
        stats = split.run_split(unload_path=unload_file, out_dir=out_dir, encoding_opt="utf-8",
                                 chunk_bytes=8 * 1024 * 1024, sniff_bytes=4 * 1024 * 1024, limit_bytes=cut_at)
        rows = [json.loads(l) for l in (out_dir / "objects.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["truncated"])
        self.assertTrue(rows[1]["truncated"])

    def test_small_chunk_size_does_not_split_multibyte_utf8_hebrew(self):
        # Regression guard for the streaming decoder: an 8 MiB chunk boundary
        # (or here, a deliberately tiny one) landing mid-way through a
        # multi-byte UTF-8 character must not corrupt or crash -- this is
        # exactly why utf-8 gets a real incremental decoder in
        # _make_chunk_decoder() instead of decoding each raw chunk alone.
        hebrew_payload = "שלום עולם " * 5
        lines = one_object("RC", "HEB1", "F", extra_source=[hebrew_payload, "END-DEFINE"])
        stats, out_dir, rows = self._run(lines, chunk_bytes=7)  # pathologically small
        self.assertEqual(stats.objects_seen, 1)
        content = (out_dir / rows[0]["source_path"]).read_text(encoding="utf-8")
        self.assertIn(hebrew_payload.strip(), content)

    def test_record_length_not_multiple_of_12_is_counted_not_fatal(self):
        lines = [c_line("RC", "BADPAD", "F") + "X",  # break the padding on purpose
                 d01_line("RC", "BADPAD", "F"), d02_line("0" * 15, "0" * 15, " " * 10),
                 d03_line(), d04_line(), s_line("X")]
        stats, out_dir, rows = self._run(lines)
        self.assertGreaterEqual(stats.record_pad_bad, 1)
        self.assertEqual(stats.objects_seen, 1)  # still parses fine


if __name__ == "__main__":
    unittest.main()
