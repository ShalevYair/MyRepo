import unittest

import _pathsetup  # noqa: F401
from natlib import objid


class TestObjectId(unittest.TestCase):
    def test_normalize_uppercases_and_joins(self):
        self.assertEqual(objid.normalize_object_id("rc", "go0701p0"), "RC/GO0701P0")

    def test_normalize_strips_whitespace(self):
        self.assertEqual(objid.normalize_object_id("  RC  ", "  GO0701P0  "), "RC/GO0701P0")

    def test_split_round_trips(self):
        oid = objid.normalize_object_id("RCOLD", "GO0701P0")
        self.assertEqual(objid.split_object_id(oid), ("RCOLD", "GO0701P0"))

    def test_split_returns_none_for_legacy_name_only_id(self):
        self.assertIsNone(objid.split_object_id("GO0701P0"))

    def test_distinguishes_same_name_different_library(self):
        # This is the exact failure mode MERGE-PLAN.md documents: the old
        # viewer's name-only key merges RC/GO0701P0 and RCOLD/GO0701P0.
        a = objid.normalize_object_id("RC", "GO0701P0")
        b = objid.normalize_object_id("RCOLD", "GO0701P0")
        self.assertNotEqual(a, b)


class TestNormalizeSource(unittest.TestCase):
    def test_collapses_internal_whitespace_runs(self):
        self.assertEqual(objid.normalize_source("A    B\tC"), "A B C")

    def test_strips_trailing_whitespace_per_line(self):
        self.assertEqual(objid.normalize_source("ABC   \nDEF\t\n"), "ABC\nDEF")

    def test_strips_leading_whitespace_per_line(self):
        # Indentation is treated the same as trailing padding — see the
        # docstring on normalize_source() for why.
        self.assertEqual(objid.normalize_source("    ABC\n\tDEF\n"), "ABC\nDEF")

    def test_drops_empty_lines(self):
        self.assertEqual(objid.normalize_source("A\n\n\nB\n"), "A\nB")

    def test_empty_input(self):
        self.assertEqual(objid.normalize_source(""), "")


class TestHashing(unittest.TestCase):
    def test_sha256_norm_is_64_hex_chars(self):
        h = objid.sha256_norm("DEFINE DATA\nEND-DEFINE\n")
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_sha256_norm_ignores_indentation_padding_and_blank_lines(self):
        # The whole point of sha256_norm (SCHEMAS.md section 6, MERGE-PLAN.md
        # stage-2 duplication analysis): two copies that differ only in
        # incidental whitespace -- leading, trailing, or extra blank lines
        # -- must hash identically.
        a = "DEFINE DATA\n  WRITE 'HELLO'   \n\n\nEND-DEFINE\n"
        b = "DEFINE DATA\nWRITE 'HELLO'\nEND-DEFINE"
        self.assertEqual(objid.sha256_norm(a), objid.sha256_norm(b))

    def test_sha256_raw_does_not_ignore_whitespace_differences(self):
        # sha256_raw is the honest "did the bytes change at all" hash —
        # it must NOT collapse the same two texts sha256_norm treats as equal.
        a = "DEFINE DATA\n  WRITE 'HELLO'   \n\n\nEND-DEFINE\n"
        b = "DEFINE DATA\nWRITE 'HELLO'\nEND-DEFINE"
        self.assertNotEqual(objid.sha256_raw(a), objid.sha256_raw(b))

    def test_sha256_norm_distinguishes_actually_different_content(self):
        a = objid.sha256_norm("WRITE 'HELLO'")
        b = objid.sha256_norm("WRITE 'GOODBYE'")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
