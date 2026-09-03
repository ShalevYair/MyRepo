import unittest

import _pathsetup  # noqa: F401
from natlib import natprofile, report_profile


def make_report_row(status="UNLOADED", library="ADLDMF", name="ADBGLOBA",
                     type_word="GLOBAL", sc="SRC", dbidfnr="240/9",
                     date="1993-05-20", time="10:14:42", user="L9902D"):
    """Builds a 132-char fixed-column row per README.md's field table,
    control char + STATUS + LIBRARY + OBJECT NAME + TYPE + S/C + DBID/FNR
    + DATE + TIME + USER ID, at the exact offsets report_profile.ROW uses."""
    row = [" "] * 132
    def place(field, value):
        offset, length = report_profile.ROW[field]
        for i, ch in enumerate(value[:length]):
            row[offset + i] = ch
    place("status", status)
    place("library", library)
    place("name", name)
    place("type", type_word)
    place("sc", sc)
    place("dbidfnr", dbidfnr)
    place("date", date)
    place("time", time)
    place("user", user)
    return "".join(row)


class TestIsReportDataRow(unittest.TestCase):
    def test_real_shape_row_recognised(self):
        self.assertTrue(report_profile.is_report_data_row(make_report_row()))

    def test_recognised_regardless_of_status_text(self):
        # Structural fingerprint (leading space + DATE column), not the
        # literal "UNLOADED" string — a failed row must still be caught.
        self.assertTrue(report_profile.is_report_data_row(make_report_row(status="ERROR")))

    def test_too_short_rejected(self):
        self.assertFalse(report_profile.is_report_data_row("short line"))

    def test_wrong_control_char_rejected(self):
        row = make_report_row()
        row = "0" + row[1:]  # ASA double-space control char instead of single space
        self.assertFalse(report_profile.is_report_data_row(row))

    def test_malformed_date_rejected(self):
        row = list(make_report_row())
        offset, _ = report_profile.ROW["date"]
        row[offset:offset + 10] = list("not-a-date")
        self.assertFalse(report_profile.is_report_data_row("".join(row)))


class TestSniffFileProfile(unittest.TestCase):
    def test_raw_unload_sample(self):
        text = "*H**NATPROG header line\n*C**object catalog entry\n"
        self.assertEqual(report_profile.sniff_file_profile(text), "natural-sysobjh")

    def test_report_sample(self):
        text = "\n".join(make_report_row(name=f"OBJ{i}") for i in range(3))
        self.assertEqual(report_profile.sniff_file_profile(text), "natural-sysobjh-report")

    def test_banner_alone_is_enough(self):
        text = "some header\n   NATURAL OBJECT HANDLER  V8.2.07  2026-08-13\nmore text\n"
        self.assertEqual(report_profile.sniff_file_profile(text), "natural-sysobjh-report")

    def test_unrelated_text_is_none(self):
        text = "just some ordinary text file\nwith a few lines\nnothing special\n"
        self.assertEqual(report_profile.sniff_file_profile(text), "none")


class TestCrossProfileConsistency(unittest.TestCase):
    """The two profiles describe the same repository (README.md's whole
    premise for the cross-check tab) — every type word the report profile
    knows must map to a letter the raw-unload profile also knows, or the
    two tools will silently disagree about a real object's type."""

    def test_every_report_type_word_maps_to_a_known_letter(self):
        for word, letter in report_profile.REPORT_TYPE_TO_LETTER.items():
            self.assertIn(letter, natprofile.TYPE_MAP, f"{word} -> {letter} not in TYPE_MAP")

    def test_every_confirmed_exec_data_letter_has_a_report_word(self):
        # Only the two 'none'-confidence unknowns (7, 5) are expected to be
        # missing a report-side word; everything else should round-trip.
        letters_with_words = set(report_profile.REPORT_TYPE_TO_LETTER.values())
        for letter, meta in natprofile.TYPE_MAP.items():
            if meta["confidence"] in ("confirmed", "inferred"):
                self.assertIn(letter, letters_with_words, letter)


if __name__ == "__main__":
    unittest.main()
