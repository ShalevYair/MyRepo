import unittest

import _pathsetup  # noqa: F401
from natlib import natprofile


class TestRtrimAndFld(unittest.TestCase):
    def test_rtrim_strips_only_ascii_spaces(self):
        self.assertEqual(natprofile.rtrim("ABC   "), "ABC")
        self.assertEqual(natprofile.rtrim("ABC\t"), "ABC\t")  # tabs are not stripped
        self.assertEqual(natprofile.rtrim(""), "")
        self.assertEqual(natprofile.rtrim("   "), "")

    def test_fld_extracts_and_rtrims(self):
        # *C** record: library@36:8, name@44:32, type@76:1 (README.md table)
        line = "*C**" + " " * 32 + "ADLDMF  " + "ADBGLOBA" + " " * 24 + "C"
        self.assertEqual(natprofile.fld(line, natprofile.C["library"]), "ADLDMF")
        self.assertEqual(natprofile.fld(line, natprofile.C["name"]), "ADBGLOBA")
        self.assertEqual(natprofile.fld(line, natprofile.C["type"]), "C")


class TestParseNatTs(unittest.TestCase):
    def test_valid_timestamp(self):
        # 2015-03-08 15:00:15, tenths=0
        result = natprofile.parse_nat_ts("201503081500150")
        self.assertTrue(result["ok"])
        self.assertEqual(result["iso"], "2015-03-08T15:00:15")
        self.assertEqual(result["year"], 2015)

    def test_all_zero_is_empty_not_an_error(self):
        result = natprofile.parse_nat_ts("000000000000000")
        self.assertFalse(result["ok"])
        self.assertTrue(result["empty"])

    def test_wrong_length_is_not_ok(self):
        result = natprofile.parse_nat_ts("2015030815001")
        self.assertFalse(result["ok"])
        self.assertFalse(result["empty"])

    def test_non_digit_is_not_ok(self):
        result = natprofile.parse_nat_ts("2015030815AB150")
        self.assertFalse(result["ok"])

    def test_impossible_date_is_not_ok(self):
        result = natprofile.parse_nat_ts("201513321500150")  # month 13, day 32
        self.assertFalse(result["ok"])

    def test_year_out_of_range_is_not_ok(self):
        result = natprofile.parse_nat_ts("195012311500150")  # year 1950 < 1960
        self.assertFalse(result["ok"])


class TestTypeMap(unittest.TestCase):
    def test_confirmed_letters_from_readme(self):
        # README.md "Object type letters" table — these nine are confirmed
        # via cross-reference against a real SYSOBJH job-log report.
        confirmed = "FNSMLPCTV"
        for letter in confirmed:
            self.assertIn(letter, natprofile.TYPE_MAP)
            self.assertEqual(natprofile.TYPE_MAP[letter]["confidence"], "confirmed", letter)

    def test_g_is_copycode_not_global_data_area(self):
        # README.md is explicit this was the original wrong guess.
        self.assertEqual(natprofile.TYPE_MAP["G"]["name"], "Copycode")
        self.assertEqual(natprofile.TYPE_MAP["G"]["kind"], "exec")

    def test_unresolved_letters_have_no_kind(self):
        for letter in ("7", "5"):
            self.assertIsNone(natprofile.TYPE_MAP[letter]["kind"])
            self.assertEqual(natprofile.TYPE_MAP[letter]["confidence"], "none")

    def test_v_is_data_not_exec(self):
        # A DDM describes a field layout, not executable Natural.
        self.assertEqual(natprofile.TYPE_MAP["V"]["kind"], "data")


if __name__ == "__main__":
    unittest.main()
