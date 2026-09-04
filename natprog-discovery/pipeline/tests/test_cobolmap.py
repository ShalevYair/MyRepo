import json
import pathlib
import tempfile
import unittest

import _pathsetup  # noqa: F401
import cobolmap


# README.md's own validated shape (confirmed against two real files, one
# plain batch and one CICS transaction): a batch program has ordinary
# CALL 'name' USING ... subroutine calls; a CICS transaction additionally
# declares "01 SAP-OPTIONS TPMONITOR UTP-CICS." and can use LINK/XCTL/START.
BATCH_PROGRAM = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. BATCHP1.\n"
    "       PROCEDURE DIVISION.\n"
    "           CALL 'HELPER1' USING WS-BUF.\n"
    "           CALL 'HELPER2' USING WS-BUF.\n"
)

CICS_TRANSACTION = (
    "       IDENTIFICATION DIVISION.\n"
    "       PROGRAM-ID. CICSTX1.\n"
    "       01 SAP-OPTIONS TPMONITOR UTP-CICS.\n"
    "       PROCEDURE DIVISION.\n"
    "           EXEC CICS LINK PROGRAM('LINKED1') COMMAREA(WS-BUF) END-EXEC.\n"
    "           EXEC CICS XCTL PROGRAM('XCTLD1') END-EXEC.\n"
    "           EXEC CICS START TRANSID('TX02') END-EXEC.\n"
)


class TestParseCobolBasics(unittest.TestCase):
    def test_readme_batch_example(self):
        r = cobolmap.parse_cobol(BATCH_PROGRAM, "BATCHP1.cbl")
        self.assertEqual(r["program_id"], "BATCHP1")
        self.assertFalse(r["uses_cics"])
        self.assertEqual([c["kind"] for c in r["calls"]], ["call", "call"])
        self.assertEqual([c["target"] for c in r["calls"]], ["HELPER1", "HELPER2"])

    def test_readme_cics_example_all_four_kinds(self):
        r = cobolmap.parse_cobol(CICS_TRANSACTION, "CICSTX1.cbl")
        self.assertEqual(r["program_id"], "CICSTX1")
        self.assertTrue(r["uses_cics"])
        kinds = {c["kind"]: c["target"] for c in r["calls"]}
        self.assertEqual(kinds, {"cics-link": "LINKED1", "cics-xctl": "XCTLD1", "cics-start": "TX02"})

    def test_program_id_case_insensitive_keyword_uppercased_target(self):
        text = "       program-id. lowerp1.\n"
        r = cobolmap.parse_cobol(text, "F.cbl")
        self.assertEqual(r["program_id"], "LOWERP1")

    def test_comment_line_with_leading_spaces_skipped(self):
        # COBOL column-7 '*' -- leading whitespace before it is normal, unlike
        # Natural's strict column-1 rule used on the other side of the bridge.
        text = (
            "       PROGRAM-ID. P1.\n"
            "      * CALL 'SHOULDNOTCOUNT'\n"
            "           CALL 'REAL1'.\n"
        )
        r = cobolmap.parse_cobol(text, "F.cbl")
        self.assertEqual([c["target"] for c in r["calls"]], ["REAL1"])

    def test_first_program_id_wins_if_repeated(self):
        text = "       PROGRAM-ID. FIRST1.\n       PROGRAM-ID. SECOND1.\n"
        r = cobolmap.parse_cobol(text, "F.cbl")
        self.assertEqual(r["program_id"], "FIRST1")

    def test_no_program_id_present(self):
        r = cobolmap.parse_cobol("           CALL 'X'.\n", "NOID.cbl")
        self.assertIsNone(r["program_id"])

    def test_multiple_calls_same_line(self):
        text = "       PROGRAM-ID. P1.\n           IF X CALL 'A' ELSE CALL 'B'.\n"
        r = cobolmap.parse_cobol(text, "F.cbl")
        self.assertEqual([c["target"] for c in r["calls"]], ["A", "B"])

    def test_link_program_double_quoted_variant(self):
        text = "       PROGRAM-ID. P1.\n           EXEC CICS LINK PROGRAM(\"DBLQ1\") END-EXEC.\n"
        r = cobolmap.parse_cobol(text, "F.cbl")
        self.assertEqual(r["calls"], [{"kind": "cics-link", "target": "DBLQ1"}])


class TestAnalyzeCobolAndBuildJson(unittest.TestCase):
    def test_self_contained_folder_resolves_internal_calls(self):
        # Mirrors README.md's documented cross-check: resolution only works
        # well when the caller AND the callee are both in the folder.
        caller = cobolmap.parse_cobol("       PROGRAM-ID. CALLER1.\n           CALL 'CALLEE1'.\n", "CALLER1.cbl")
        callee = cobolmap.parse_cobol("       PROGRAM-ID. CALLEE1.\n", "CALLEE1.cbl")
        analysis = cobolmap.analyze_cobol([caller, callee])
        self.assertEqual(analysis["resolution"], {"resolved": 1, "unresolved": 0, "total": 1})

    def test_readme_two_file_scenario_zero_resolved_then_synthetic_third_resolves(self):
        # README.md: "two real sample files (0/18 resolved, correctly --
        # neither file calls the other)... a synthetic third file declaring
        # PROGRAM-ID. INVERSE. added to the same set: resolution correctly
        # jumped to 14/18". Reproduced at small scale with the same shape:
        # two files that call an external name neither defines, then a third
        # file that defines it.
        file_a = cobolmap.parse_cobol(
            "       PROGRAM-ID. FILEA.\n           CALL 'INVERSE'.\n           CALL 'INVERSE'.\n", "A.cbl")
        file_b = cobolmap.parse_cobol(
            "       PROGRAM-ID. FILEB.\n           CALL 'INVERSE'.\n", "B.cbl")
        before = cobolmap.analyze_cobol([file_a, file_b])
        self.assertEqual(before["resolution"], {"resolved": 0, "unresolved": 3, "total": 3})

        inverse = cobolmap.parse_cobol("       PROGRAM-ID. INVERSE.\n", "INVERSE.cbl")
        after = cobolmap.analyze_cobol([file_a, file_b, inverse])
        self.assertEqual(after["resolution"], {"resolved": 3, "unresolved": 0, "total": 3})

    def test_cics_start_never_counted_as_unresolved(self):
        r = cobolmap.parse_cobol(CICS_TRANSACTION, "CICSTX1.cbl")
        analysis = cobolmap.analyze_cobol([r])
        # 3 real calls (link/xctl/start), only 2 are resolvable (start excluded)
        self.assertEqual(analysis["resolution"]["total"], 2)
        self.assertEqual(len(analysis["cics_start_rows"]), 1)
        self.assertEqual(analysis["cics_start_rows"][0]["target"], "TX02")

    def test_build_cobol_json_schema_shape(self):
        parsed = [cobolmap.parse_cobol(CICS_TRANSACTION, "CICSTX1.cbl")]
        analysis = cobolmap.analyze_cobol(parsed)
        j = cobolmap.build_cobol_json(parsed, analysis, natural_bridge=[])
        self.assertEqual(set(j.keys()), {"programs", "calls", "cics_starts", "natural_bridge"})
        self.assertEqual(j["programs"], [{"file": "CICSTX1.cbl", "program_id": "CICSTX1", "uses_cics": True}])
        # calls[] carries found_in_folder; cics_starts[] does not.
        for c in j["calls"]:
            self.assertEqual(set(c.keys()), {"file", "from", "kind", "target", "found_in_folder"})
            self.assertNotEqual(c["kind"], "cics-start")
        for c in j["cics_starts"]:
            self.assertEqual(set(c.keys()), {"file", "from", "target"})

    def test_found_in_folder_false_for_genuinely_external_target(self):
        r = cobolmap.parse_cobol("       PROGRAM-ID. P1.\n           CALL 'NOWHERE'.\n", "P1.cbl")
        analysis = cobolmap.analyze_cobol([r])
        j = cobolmap.build_cobol_json([r], analysis, natural_bridge=[])
        self.assertEqual(j["calls"][0]["found_in_folder"], False)


class TestBuildSummary(unittest.TestCase):
    def test_counts_match_app_js_analyzecobol_shape(self):
        caller = cobolmap.parse_cobol("       PROGRAM-ID. CALLER1.\n           CALL 'CALLEE1'.\n", "CALLER1.cbl")
        cics = cobolmap.parse_cobol(CICS_TRANSACTION, "CICSTX1.cbl")
        parsed = [caller, cics]
        analysis = cobolmap.analyze_cobol(parsed)
        s = cobolmap.build_summary(parsed, analysis)
        self.assertEqual(s["files_parsed"], 2)
        self.assertEqual(s["programs_with_id"], 2)
        self.assertEqual(s["cics_programs"], 1)
        # 1 (call) + 3 (link/xctl/start) = 4 total, matching app.js totalCalls
        # (counts every kind, cics-start included).
        self.assertEqual(s["total_calls"], 4)
        self.assertEqual(s["cics_starts_count"], 1)

    def test_top_n_sorts_by_count_desc_then_key_asc(self):
        c = cobolmap.Counter({"B": 2, "A": 2, "C": 1})
        self.assertEqual(cobolmap._top_n(c, 10), [["A", 2], ["B", 2], ["C", 1]])


class TestNaturalBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = pathlib.Path(self.tmp.name) / "out"
        (self.out_dir / "source" / "RC").mkdir(parents=True)

    def _write_source(self, rel_path: str, lines: list[str]) -> None:
        p = self.out_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_objects_jsonl(self, rows: list[dict]) -> pathlib.Path:
        p = self.out_dir / "objects.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_match_found_and_case_preserved_in_raw_target(self):
        self._write_source("source/RC/GO0701P0.nat", ["CALLNAT 'X'.", "CALL 'MixedCase1' USING #A."])
        objects_path = self._write_objects_jsonl([
            {"object_id": "RC/GO0701P0", "source_path": "source/RC/GO0701P0.nat"},
        ])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index={"MIXEDCASE1"})
        self.assertTrue(result["enabled"])
        self.assertEqual(result["objects_scanned"], 1)
        self.assertEqual(len(result["matches"]), 1)
        m = result["matches"][0]
        self.assertEqual(m["cobol_program"], "MIXEDCASE1")
        self.assertEqual(m["natural_call_target"], "MixedCase1")  # original case preserved
        self.assertEqual(m["natural_object_id"], "RC/GO0701P0")
        self.assertEqual(m["natural_source_path"], "source/RC/GO0701P0.nat")

    def test_comment_line_not_scanned(self):
        # Natural comment: literal '*' in column 1, no leading whitespace
        # allowed -- distinct from COBOL's column-7 rule (see module docstring).
        self._write_source("source/RC/P1.nat", ["*CALL 'SHOULDNOTMATCH'."])
        objects_path = self._write_objects_jsonl([{"object_id": "RC/P1", "source_path": "source/RC/P1.nat"}])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index={"SHOULDNOTMATCH"})
        self.assertEqual(result["matches"], [])

    def test_target_not_in_program_index_is_not_a_match(self):
        self._write_source("source/RC/P1.nat", ["CALL 'NOTCOBOL'."])
        objects_path = self._write_objects_jsonl([{"object_id": "RC/P1", "source_path": "source/RC/P1.nat"}])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index={"SOMETHINGELSE"})
        self.assertEqual(result["matches"], [])

    def test_inline_comment_after_call_still_matches(self):
        self._write_source("source/RC/P1.nat", ["CALL 'REAL1' /* inline note"])
        objects_path = self._write_objects_jsonl([{"object_id": "RC/P1", "source_path": "source/RC/P1.nat"}])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index={"REAL1"})
        self.assertEqual(len(result["matches"]), 1)

    def test_missing_objects_jsonl_skips_gracefully(self):
        missing = self.out_dir / "objects.jsonl"
        result = cobolmap.extract_natural_bridge(missing, self.out_dir, program_index={"X"})
        self.assertFalse(result["enabled"])
        self.assertIn("not found", result["skipped_reason"])
        self.assertEqual(result["matches"], [])

    def test_empty_program_index_skips_without_reading_files(self):
        objects_path = self._write_objects_jsonl([{"object_id": "RC/P1", "source_path": "source/RC/P1.nat"}])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index=set())
        self.assertFalse(result["enabled"])
        self.assertEqual(result["objects_scanned"], 0)

    def test_missing_individual_source_file_recorded_not_raised(self):
        objects_path = self._write_objects_jsonl([{"object_id": "RC/GONE", "source_path": "source/RC/GONE.nat"}])
        result = cobolmap.extract_natural_bridge(objects_path, self.out_dir, program_index={"X"})
        self.assertEqual(result["objects_scanned"], 1)
        self.assertEqual(len(result["read_errors"]), 1)
        self.assertEqual(result["read_errors"][0]["object_id"], "RC/GONE")


class TestMainCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def _run(self, extra_args):
        import contextlib
        stdout_path = self.tmp_path / "stdout.json"
        with stdout_path.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
            rc = cobolmap.main(extra_args)
        summary = json.loads(stdout_path.read_text(encoding="utf-8")) if stdout_path.stat().st_size else None
        return rc, summary

    def test_end_to_end_writes_cobol_json_and_prints_summary(self):
        cobol_dir = self.tmp_path / "cobol"
        cobol_dir.mkdir()
        (cobol_dir / "BATCHP1.cbl").write_text(BATCH_PROGRAM, encoding="utf-8")
        (cobol_dir / "CICSTX1.cbl").write_text(CICS_TRANSACTION, encoding="utf-8")
        out_dir = self.tmp_path / "out"

        rc, summary = self._run(["--cobol-dir", str(cobol_dir), "--out-dir", str(out_dir), "--encoding", "utf-8"])
        self.assertEqual(rc, 0)

        cobol_json_path = out_dir / "cobol.json"
        self.assertTrue(cobol_json_path.is_file())
        cobol_json = json.loads(cobol_json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cobol_json["programs"]), 2)

        self.assertEqual(summary["files_parsed"], 2)
        self.assertFalse(summary["natural_bridge"]["enabled"])  # no objects.jsonl in out_dir

    def test_missing_cobol_dir_errors_cleanly(self):
        rc, _ = self._run(["--cobol-dir", str(self.tmp_path / "nope"), "--out-dir", str(self.tmp_path / "out")])
        self.assertEqual(rc, 1)

    def test_recurses_into_subdirectories(self):
        cobol_dir = self.tmp_path / "cobol"
        (cobol_dir / "sub").mkdir(parents=True)
        (cobol_dir / "sub" / "NESTED.cbl").write_text(BATCH_PROGRAM, encoding="utf-8")
        out_dir = self.tmp_path / "out"
        rc, summary = self._run(["--cobol-dir", str(cobol_dir), "--out-dir", str(out_dir), "--encoding", "utf-8"])
        self.assertEqual(rc, 0)
        self.assertEqual(summary["files_parsed"], 1)

    def test_bridge_runs_automatically_when_objects_jsonl_present(self):
        cobol_dir = self.tmp_path / "cobol"
        cobol_dir.mkdir()
        (cobol_dir / "BATCHP1.cbl").write_text(BATCH_PROGRAM, encoding="utf-8")
        out_dir = self.tmp_path / "out"
        (out_dir / "source" / "RC").mkdir(parents=True)
        # BATCHP1.cbl's own PROGRAM-ID is BATCHP1 -- that's the only name
        # this COBOL folder can resolve a bridge match against.
        (out_dir / "source" / "RC" / "CALLER.nat").write_text("CALL 'BATCHP1'.\n", encoding="utf-8")
        with (out_dir / "objects.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"object_id": "RC/CALLER", "source_path": "source/RC/CALLER.nat"}) + "\n")

        rc, summary = self._run(["--cobol-dir", str(cobol_dir), "--out-dir", str(out_dir), "--encoding", "utf-8"])
        self.assertEqual(rc, 0)
        self.assertTrue(summary["natural_bridge"]["enabled"])
        self.assertEqual(summary["natural_bridge"]["matches_found"], 1)

        cobol_json = json.loads((out_dir / "cobol.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cobol_json["natural_bridge"]), 1)
        self.assertEqual(cobol_json["natural_bridge"][0]["cobol_program"], "BATCHP1")

    def test_skip_bridge_flag_disables_it_even_with_objects_jsonl(self):
        cobol_dir = self.tmp_path / "cobol"
        cobol_dir.mkdir()
        (cobol_dir / "BATCHP1.cbl").write_text(BATCH_PROGRAM, encoding="utf-8")
        out_dir = self.tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "objects.jsonl").write_text("", encoding="utf-8")

        rc, summary = self._run(["--cobol-dir", str(cobol_dir), "--out-dir", str(out_dir),
                                  "--encoding", "utf-8", "--skip-bridge"])
        self.assertEqual(rc, 0)
        self.assertFalse(summary["natural_bridge"]["enabled"])
        self.assertEqual(summary["natural_bridge"]["skipped_reason"], "--skip-bridge passed")


if __name__ == "__main__":
    unittest.main()
