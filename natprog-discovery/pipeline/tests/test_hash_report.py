import json
import pathlib
import tempfile
import unittest

import _pathsetup  # noqa: F401
import hash_report


def obj(object_id, library, sha_norm, **extra):
    row = {"object_id": object_id, "library": library, "sha256_norm": sha_norm}
    row.update(extra)
    return row


class TestBuildReportBasics(unittest.TestCase):
    def test_empty_input_does_not_crash(self):
        report = hash_report.build_report([], [])
        self.assertEqual(report["total_objects"], 0)
        self.assertEqual(report["distinct_content_families"], 0)
        self.assertIsNone(report["duplicate_object_ratio"])
        self.assertEqual(report["shadow_copy"]["family_count"], 0)
        self.assertEqual(report["graveyard_candidate_libraries"]["count"], 0)

    def test_all_unique_content_has_zero_duplicates(self):
        objects = [obj(f"RC/P{i}", "RC", f"hash{i}") for i in range(5)]
        report = hash_report.build_report(objects, [])
        self.assertEqual(report["total_objects"], 5)
        self.assertEqual(report["distinct_content_families"], 5)
        self.assertEqual(report["singleton_families"], 5)
        self.assertEqual(report["duplicate_objects"], 0)
        self.assertEqual(report["duplicate_object_ratio"], 0.0)
        self.assertEqual(report["shadow_copy"]["family_count"], 0)

    def test_duplicate_ratio_counts_non_representative_members(self):
        # 5 objects, 3 distinct hashes (one hash shared by 3 objects) ->
        # 3 families, 2 "extra" copies beyond one representative each.
        objects = [
            obj("RC/A", "RC", "H1"), obj("RC/B", "RC", "H1"), obj("RC/C", "RC", "H1"),
            obj("RC/D", "RC", "H2"),
            obj("RC/E", "RC", "H3"),
        ]
        report = hash_report.build_report(objects, [])
        self.assertEqual(report["total_objects"], 5)
        self.assertEqual(report["distinct_content_families"], 3)
        self.assertEqual(report["duplicate_objects"], 2)
        self.assertEqual(report["duplicate_object_ratio"], 0.4)

    def test_missing_sha256_norm_is_skipped_not_crashed(self):
        objects = [obj("RC/A", "RC", "H1"), {"object_id": "RC/B", "library": "RC"}]
        report = hash_report.build_report(objects, [])
        self.assertEqual(report["total_objects"], 2)
        self.assertEqual(report["distinct_content_families"], 1)


class TestShadowCopy(unittest.TestCase):
    def test_same_library_duplicate_is_not_shadow_copy(self):
        # Two objects, same hash, same library: real duplication, but not
        # the cross-library "shadow copy" kind WORKPLAN.md 2.1 asks for.
        objects = [obj("RC/A", "RC", "H1"), obj("RC/B", "RC", "H1")]
        report = hash_report.build_report(objects, [])
        self.assertEqual(report["duplicate_objects"], 1)
        self.assertEqual(report["shadow_copy"]["family_count"], 0)

    def test_cross_library_duplicate_is_shadow_copy(self):
        objects = [obj("RC/A", "RC", "H1"), obj("RCOLD/A", "RCOLD", "H1")]
        report = hash_report.build_report(objects, [])
        sc = report["shadow_copy"]
        self.assertEqual(sc["family_count"], 1)
        self.assertEqual(sc["object_count"], 2)
        fam = sc["top_families"][0]
        self.assertEqual(fam["sha256_norm"], "H1")
        self.assertEqual(fam["family_size"], 2)
        self.assertEqual(fam["libraries"], ["RC", "RCOLD"])
        self.assertIn("RC/A", fam["sample_object_ids"])
        self.assertIn("RCOLD/A", fam["sample_object_ids"])

    def test_top_families_sorted_by_size_descending(self):
        objects = (
            [obj(f"A/x{i}", "A", "BIG") for i in range(3)]
            + [obj(f"B/x{i}", "B", "BIG") for i in range(3)]
            + [obj("C/y", "C", "SMALL"), obj("D/y", "D", "SMALL")]
        )
        report = hash_report.build_report(objects, [])
        top = report["shadow_copy"]["top_families"]
        self.assertEqual(top[0]["sha256_norm"], "BIG")
        self.assertEqual(top[0]["family_size"], 6)
        self.assertEqual(top[1]["sha256_norm"], "SMALL")

    def test_top_n_truncates_but_family_count_stays_true(self):
        objects = []
        for i in range(5):
            h = f"H{i}"
            objects.append(obj(f"A/x{i}", "A", h))
            objects.append(obj(f"B/x{i}", "B", h))
        report = hash_report.build_report(objects, [], top_n=2)
        sc = report["shadow_copy"]
        self.assertEqual(sc["family_count"], 5)
        self.assertEqual(len(sc["top_families"]), 2)
        self.assertTrue(sc["families_truncated"])


class TestGraveyardCandidates(unittest.TestCase):
    def test_library_fully_duplicated_elsewhere_is_a_candidate(self):
        # RCOLD's content is a full subset of RC's -- RC additionally has
        # H3, content found nowhere else. So RCOLD is fully explained by RC
        # (a candidate), but RC is NOT fully explained by RCOLD (it has
        # unique content RCOLD lacks). A perfectly symmetric pair would
        # legitimately flag BOTH sides -- the report can't know which one
        # is "the original" from content alone (WORKPLAN.md 2.2: this is a
        # signal for a documented human decision, not an automatic pick).
        objects = [
            obj("RCOLD/A", "RCOLD", "H1"), obj("RC/A", "RC", "H1"),
            obj("RCOLD/B", "RCOLD", "H2"), obj("RC/B", "RC", "H2"),
            obj("RC/C", "RC", "H3_UNIQUE_TO_RC"),
        ]
        report = hash_report.build_report(objects, [])
        gc = report["graveyard_candidate_libraries"]
        libs = {c["library"] for c in gc["libraries"]}
        self.assertIn("RCOLD", libs)
        self.assertNotIn("RC", libs)

    def test_library_partially_duplicated_is_not_a_candidate(self):
        # RCOLD has one object with a duplicate elsewhere and one without --
        # WORKPLAN.md 2.1 says "כל תוכנן כפול" (ALL of it), not most of it.
        objects = [
            obj("RCOLD/A", "RCOLD", "H1"), obj("RC/A", "RC", "H1"),
            obj("RCOLD/UNIQUE", "RCOLD", "H_ONLY_HERE"),
        ]
        report = hash_report.build_report(objects, [])
        libs = {c["library"] for c in report["graveyard_candidate_libraries"]["libraries"]}
        self.assertNotIn("RCOLD", libs)

    def test_duplicate_target_libraries_are_recorded(self):
        objects = [
            obj("RCOLD/A", "RCOLD", "H1"), obj("RC/A", "RC", "H1"),
            obj("RCOLD/B", "RCOLD", "H2"), obj("RC1/B", "RC1", "H2"),
        ]
        report = hash_report.build_report(objects, [])
        rcold = next(c for c in report["graveyard_candidate_libraries"]["libraries"] if c["library"] == "RCOLD")
        self.assertEqual(rcold["duplicate_target_libraries"], ["RC", "RC1"])

    def test_graveyard_name_pattern_cross_reference(self):
        objects = [
            obj("RCOLD/A", "RCOLD", "H1"), obj("RC/A", "RC", "H1"),
            obj("FRESHLIB/B", "FRESHLIB", "H2"), obj("RC/B", "RC", "H2"),
        ]
        patterns = [r".*OLD$"]
        report = hash_report.build_report(objects, patterns)
        by_lib = {c["library"]: c for c in report["graveyard_candidate_libraries"]["libraries"]}
        self.assertTrue(by_lib["RCOLD"]["matches_graveyard_name_pattern"])
        self.assertFalse(by_lib["FRESHLIB"]["matches_graveyard_name_pattern"])

    def test_pattern_must_match_whole_name_case_insensitively(self):
        self.assertTrue(hash_report._matches_any("rcold", hash_report._compile_graveyard_patterns([r".*OLD$"])))
        self.assertFalse(hash_report._matches_any("RCOLDER", hash_report._compile_graveyard_patterns([r".*OLD$"])))


class TestLoadObjects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def test_reads_valid_jsonl(self):
        p = self.tmp_path / "objects.jsonl"
        p.write_text(
            json.dumps({"object_id": "RC/A", "library": "RC", "sha256_norm": "H1"}) + "\n"
            + json.dumps({"object_id": "RC/B", "library": "RC", "sha256_norm": "H2"}) + "\n",
            encoding="utf-8",
        )
        objects = hash_report.load_objects(p)
        self.assertEqual(len(objects), 2)

    def test_skips_blank_lines(self):
        p = self.tmp_path / "objects.jsonl"
        p.write_text('{"object_id": "RC/A", "library": "RC", "sha256_norm": "H1"}\n\n\n', encoding="utf-8")
        objects = hash_report.load_objects(p)
        self.assertEqual(len(objects), 1)

    def test_malformed_line_raises_with_line_number(self):
        p = self.tmp_path / "objects.jsonl"
        p.write_text('{"object_id": "RC/A"}\n{not valid json\n', encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            hash_report.load_objects(p)
        self.assertIn(":2:", str(ctx.exception))


class TestMainCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def test_end_to_end_against_real_config(self):
        # Uses the repo's real config.yaml (like tests/test_config.py does)
        # for graveyard_library_patterns -- only --objects is overridden.
        p = self.tmp_path / "objects.jsonl"
        p.write_text(
            "\n".join(json.dumps(o) for o in [
                {"object_id": "RCOLD/A", "library": "RCOLD", "sha256_norm": "H1"},
                {"object_id": "RC/A", "library": "RC", "sha256_norm": "H1"},
            ]) + "\n",
            encoding="utf-8",
        )
        out_path = self.tmp_path / "stdout.json"
        import contextlib
        with out_path.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
            rc = hash_report.main(["--objects", str(p)])
        self.assertEqual(rc, 0)
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["total_objects"], 2)
        self.assertEqual(report["shadow_copy"]["family_count"], 1)

    def test_missing_objects_file_errors_cleanly(self):
        rc = hash_report.main(["--objects", str(self.tmp_path / "nope.jsonl")])
        self.assertEqual(rc, 1)

    def test_out_writes_report_to_file_as_utf8(self):
        p = self.tmp_path / "objects.jsonl"
        p.write_text(
            "\n".join(json.dumps(o) for o in [
                {"object_id": "RCOLD/A", "library": "RCOLD", "sha256_norm": "H1"},
                {"object_id": "RC/A", "library": "RC", "sha256_norm": "H1"},
            ]) + "\n",
            encoding="utf-8",
        )
        out_path = self.tmp_path / "report.json"
        rc = hash_report.main(["--objects", str(p), "--out", str(out_path)])
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.is_file())
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["total_objects"], 2)

    def test_out_creates_missing_parent_directories(self):
        p = self.tmp_path / "objects.jsonl"
        p.write_text(json.dumps({"object_id": "RC/A", "library": "RC", "sha256_norm": "H1"}) + "\n", encoding="utf-8")
        out_path = self.tmp_path / "nested" / "dir" / "report.json"
        rc = hash_report.main(["--objects", str(p), "--out", str(out_path)])
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.is_file())


if __name__ == "__main__":
    unittest.main()
