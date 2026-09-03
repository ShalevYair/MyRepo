import json
import pathlib
import tempfile
import unittest

import _pathsetup  # noqa: F401
import jclmap


# README.md's own validated example (confirmed against 5 real JCL jobs):
#     //STEP2 EXEC NATB240,COND=(0,NE)
#     //CMSYNIN DD *
#     LOGON RC
#     HICNEWN3
#     FIN
BASIC_NATURAL_BATCH = (
    "//MYJOB01 JOB (ACCT),'ME'\n"
    "//STEP2   EXEC NATB240,COND=(0,NE)\n"
    "//CMSYNIN DD *\n"
    "LOGON RC\n"
    "HICNEWN3\n"
    "FIN\n"
    "//\n"
)


class TestParseJclBasics(unittest.TestCase):
    def test_readme_example_logon_form(self):
        r = jclmap.parse_jcl(BASIC_NATURAL_BATCH, "JOB1")
        self.assertEqual(r["job_name"], "MYJOB01")
        self.assertEqual(len(r["program_refs"]), 1)
        ref = r["program_refs"][0]
        self.assertEqual(ref["kind"], "natural-batch")
        self.assertEqual(ref["library"], "RC")
        self.assertEqual(ref["program"], "HICNEWN3")
        self.assertEqual(ref["step"], "STEP2")

    def test_bare_library_form_no_logon_keyword(self):
        # README.md: "sometimes as a bare '<lib>' with no LOGON keyword --
        # both forms appear in the same job."
        text = (
            "//MYJOB02 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC NATB240\n"
            "//CMSYNIN DD *\n"
            "RC\n"
            "HICNEWN3\n"
            "FIN\n"
        )
        r = jclmap.parse_jcl(text, "JOB2")
        self.assertEqual(len(r["program_refs"]), 1)
        self.assertEqual(r["program_refs"][0]["library"], "RC")
        self.assertEqual(r["program_refs"][0]["program"], "HICNEWN3")

    def test_multiple_programs_in_one_cmsynin_block(self):
        text = (
            "//MYJOB03 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC NATB240\n"
            "//CMSYNIN DD *\n"
            "LOGON RC\n"
            "PROG1\n"
            "PROG2\n"
            "PROG3\n"
            "FIN\n"
        )
        r = jclmap.parse_jcl(text, "JOB3")
        programs = [p["program"] for p in r["program_refs"]]
        self.assertEqual(programs, ["PROG1", "PROG2", "PROG3"])
        self.assertTrue(all(p["library"] == "RC" for p in r["program_refs"]))

    def test_cmsynin_terminated_by_instream_end_marker(self):
        text = (
            "//MYJOB04 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC NATB240\n"
            "//CMSYNIN DD *\n"
            "LOGON RC\n"
            "HICNEWN3\n"
            "/*\n"
            "//STEP2   EXEC PGM=SORT\n"
        )
        r = jclmap.parse_jcl(text, "JOB4")
        self.assertEqual(len(r["program_refs"]), 2)
        self.assertEqual(r["program_refs"][0]["kind"], "natural-batch")
        self.assertEqual(r["program_refs"][1]["kind"], "direct-pgm")
        self.assertEqual(r["program_refs"][1]["program"], "SORT")

    def test_direct_pgm_step_recorded_without_library(self):
        text = "//MYJOB05 JOB (ACCT),'ME'\n//STEP1   EXEC PGM=FRENPGM\n"
        r = jclmap.parse_jcl(text, "JOB5")
        self.assertEqual(len(r["program_refs"]), 1)
        ref = r["program_refs"][0]
        self.assertEqual(ref["kind"], "direct-pgm")
        self.assertIsNone(ref["library"])
        self.assertEqual(ref["program"], "FRENPGM")

    def test_proc_step_recorded_in_steps_but_not_a_program_ref(self):
        text = "//MYJOB06 JOB (ACCT),'ME'\n//STEP1   EXEC SOMEPROC,PARM=X\n"
        r = jclmap.parse_jcl(text, "JOB6")
        self.assertEqual(r["steps"], [{"step": "STEP1", "kind": "proc", "target": "SOMEPROC"}])
        self.assertEqual(r["program_refs"], [])

    def test_job_card_with_hash_prefix(self):
        text = "//#A3MIVH1 JOB (ACCT),'ME'\n"
        r = jclmap.parse_jcl(text, "JOB7")
        self.assertEqual(r["job_name"], "A3MIVH1")

    def test_comment_lines_and_commented_out_exec_are_skipped(self):
        text = (
            "//MYJOB08 JOB (ACCT),'ME'\n"
            "//* this is a comment\n"
            "//*STEP1   EXEC PGM=SORT\n"
            "//STEP2   EXEC PGM=FTP\n"
        )
        r = jclmap.parse_jcl(text, "JOB8")
        self.assertEqual(len(r["program_refs"]), 1)
        self.assertEqual(r["program_refs"][0]["program"], "FTP")

    def test_no_job_card_present(self):
        text = "//STEP1   EXEC PGM=SORT\n"
        r = jclmap.parse_jcl(text, "JOB9")
        self.assertIsNone(r["job_name"])
        self.assertEqual(len(r["program_refs"]), 1)


class TestSteplibExtraction(unittest.TestCase):
    def test_single_steplib_dsn(self):
        text = (
            "//MYJOB10 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//STEPLIB DD DSN=SOME.LOAD.LIB,DISP=SHR\n"
        )
        r = jclmap.parse_jcl(text, "JOB10")
        self.assertEqual(len(r["steplib_groups"]), 1)
        self.assertEqual(r["steplib_groups"][0]["library_order"], ["SOME.LOAD.LIB"])
        self.assertEqual(r["steplib_groups"][0]["step"], "STEP1")

    def test_steplib_dd_concatenation(self):
        text = (
            "//MYJOB11 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//STEPLIB DD DSN=FIRST.LIB,DISP=SHR\n"
            "//        DD DSN=SECOND.LIB,DISP=SHR\n"
            "//        DD DSN=THIRD.LIB,DISP=SHR\n"
        )
        r = jclmap.parse_jcl(text, "JOB11")
        self.assertEqual(len(r["steplib_groups"]), 1)
        self.assertEqual(r["steplib_groups"][0]["library_order"], ["FIRST.LIB", "SECOND.LIB", "THIRD.LIB"])

    def test_natlib_dd_treated_same_as_steplib(self):
        text = (
            "//MYJOB12 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//NATLIB  DD DSN=NAT.LIB,DISP=SHR\n"
        )
        r = jclmap.parse_jcl(text, "JOB12")
        self.assertEqual(r["steplib_groups"][0]["library_order"], ["NAT.LIB"])

    def test_no_steplib_present_gives_empty_groups(self):
        r = jclmap.parse_jcl(BASIC_NATURAL_BATCH, "JOB13")
        self.assertEqual(r["steplib_groups"], [])

    def test_unrelated_dd_does_not_start_a_chain(self):
        text = (
            "//MYJOB14 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//SYSOUT  DD SYSOUT=*\n"
            "//SYSIN   DD DSN=SOME.INPUT,DISP=SHR\n"
        )
        r = jclmap.parse_jcl(text, "JOB14")
        self.assertEqual(r["steplib_groups"], [])

    def test_dsname_synonym_recognized(self):
        text = (
            "//MYJOB15 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//STEPLIB DD DSNAME=SOME.LOAD.LIB,DISP=SHR\n"
        )
        r = jclmap.parse_jcl(text, "JOB15")
        self.assertEqual(r["steplib_groups"][0]["library_order"], ["SOME.LOAD.LIB"])


class TestBuildJclJson(unittest.TestCase):
    def test_utility_refs_excluded_from_entry_points(self):
        text = "//MYJOB16 JOB (ACCT),'ME'\n//STEP1   EXEC PGM=SORT\n//STEP2   EXEC PGM=FRENPGM\n"
        parsed = [jclmap.parse_jcl(text, "JOB16")]
        j = jclmap.build_jcl_json(parsed)
        self.assertEqual(len(j["utility_refs"]), 1)
        self.assertEqual(j["utility_refs"][0]["program"], "SORT")
        self.assertEqual(len(j["entry_points"]), 1)
        self.assertEqual(j["entry_points"][0]["program"], "FRENPGM")
        self.assertEqual(j["entry_points"][0]["kind"], "direct-pgm")

    def test_entry_point_resolved_is_always_null_here(self):
        # SCHEMAS.md section 3: resolved is written by natmap3.py, not jclmap.py.
        parsed = [jclmap.parse_jcl(BASIC_NATURAL_BATCH, "JOB17")]
        j = jclmap.build_jcl_json(parsed)
        self.assertIsNone(j["entry_points"][0]["resolved"])

    def test_jobs_carry_full_step_list_not_just_a_count(self):
        parsed = [jclmap.parse_jcl(BASIC_NATURAL_BATCH, "JOB18")]
        j = jclmap.build_jcl_json(parsed)
        self.assertEqual(len(j["jobs"]), 1)
        self.assertIsInstance(j["jobs"][0]["steps"], list)
        self.assertEqual(j["jobs"][0]["steps"][0]["kind"], "proc")

    def test_steplib_chains_keyed_by_job_name_or_file(self):
        with_job = "//MYJOB19 JOB (ACCT),'ME'\n//STEP1   EXEC PGM=SORT\n//STEPLIB DD DSN=A.LIB,DISP=SHR\n"
        without_job = "//STEP1   EXEC PGM=SORT\n//STEPLIB DD DSN=B.LIB,DISP=SHR\n"
        parsed = [jclmap.parse_jcl(with_job, "F1"), jclmap.parse_jcl(without_job, "F2")]
        j = jclmap.build_jcl_json(parsed)
        by_job = {c["job"]: c["library_order"] for c in j["steplib_chains"]}
        self.assertEqual(by_job["MYJOB19"], ["A.LIB"])
        self.assertEqual(by_job["F2"], ["B.LIB"])  # falls back to file name


class TestBuildSummary(unittest.TestCase):
    def test_counts_include_utilities_matching_app_js_analyzejcl(self):
        # app.js analyzeJcl(): totalProgramRefs/naturalBatchRefs/directPgmRefs/
        # distinctPrograms/distinctLibraries are computed from ALL refs,
        # utilities included -- utilityDirectPgmCount is a separate, additional
        # breakdown, not a filter on the headline counts.
        text = (
            "//MYJOB20 JOB (ACCT),'ME'\n"
            "//STEP1   EXEC PGM=SORT\n"
            "//STEP2   EXEC PGM=FRENPGM\n"
            "//STEP3   EXEC NATB240\n"
            "//CMSYNIN DD *\n"
            "LOGON RC\n"
            "HICNEWN3\n"
            "FIN\n"
        )
        parsed = [jclmap.parse_jcl(text, "JOB20")]
        s = jclmap.build_summary(parsed)
        self.assertEqual(s["total_program_refs"], 3)
        self.assertEqual(s["natural_batch_refs"], 1)
        self.assertEqual(s["direct_pgm_refs"], 2)
        self.assertEqual(s["utility_direct_pgm_count"], 1)
        self.assertEqual(s["non_utility_direct_pgm"], ["FRENPGM"])
        self.assertEqual(s["distinct_libraries"], 1)
        # distinct_programs: "RC/HICNEWN3" (natural-batch namespace) + "SORT" +
        # "FRENPGM" (direct-pgm namespace) = 3, matching app.js's byProgram keying.
        self.assertEqual(s["distinct_programs"], 3)

    def test_utility_name_matched_case_insensitively(self):
        text = "//MYJOB21 JOB (ACCT),'ME'\n//STEP1   EXEC PGM=sort\n"
        parsed = [jclmap.parse_jcl(text, "JOB21")]
        s = jclmap.build_summary(parsed)
        self.assertEqual(s["utility_direct_pgm_count"], 1)
        self.assertEqual(s["non_utility_direct_pgm"], [])

    def test_top_n_sorts_by_count_desc_then_key_asc(self):
        c = jclmap.Counter({"B": 2, "A": 2, "C": 1})
        self.assertEqual(jclmap._top_n(c, 10), [["A", 2], ["B", 2], ["C", 1]])

    def test_jobs_with_no_job_card_counted(self):
        parsed = [jclmap.parse_jcl("//STEP1 EXEC PGM=SORT\n", "NOJOB"),
                  jclmap.parse_jcl(BASIC_NATURAL_BATCH, "HASJOB")]
        s = jclmap.build_summary(parsed)
        self.assertEqual(s["jobs_with_no_job_card"], 1)
        self.assertEqual(s["files_parsed"], 2)


class TestMainCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def test_end_to_end_writes_jcl_json_and_prints_summary(self):
        jcl_dir = self.tmp_path / "jcl"
        jcl_dir.mkdir()
        (jcl_dir / "JOB1.txt").write_text(BASIC_NATURAL_BATCH, encoding="utf-8")
        (jcl_dir / "JOB2.txt").write_text("//MYJOB99 JOB (ACCT),'ME'\n//STEP1 EXEC PGM=SORT\n", encoding="utf-8")
        out_dir = self.tmp_path / "out"

        import contextlib
        stdout_path = self.tmp_path / "stdout.json"
        with stdout_path.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
            rc = jclmap.main(["--jcl-dir", str(jcl_dir), "--out-dir", str(out_dir), "--encoding", "utf-8"])
        self.assertEqual(rc, 0)

        jcl_json_path = out_dir / "jcl.json"
        self.assertTrue(jcl_json_path.is_file())
        jcl_json = json.loads(jcl_json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(jcl_json["jobs"]), 2)

        summary = json.loads(stdout_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["files_parsed"], 2)
        self.assertEqual(summary["total_program_refs"], 2)

    def test_missing_jcl_dir_errors_cleanly(self):
        rc = jclmap.main(["--jcl-dir", str(self.tmp_path / "nope"), "--out-dir", str(self.tmp_path / "out")])
        self.assertEqual(rc, 1)

    def test_recurses_into_subdirectories(self):
        jcl_dir = self.tmp_path / "jcl"
        (jcl_dir / "sub").mkdir(parents=True)
        (jcl_dir / "sub" / "NESTED.txt").write_text(BASIC_NATURAL_BATCH, encoding="utf-8")
        out_dir = self.tmp_path / "out"
        rc = jclmap.main(["--jcl-dir", str(jcl_dir), "--out-dir", str(out_dir), "--encoding", "utf-8"])
        self.assertEqual(rc, 0)
        jcl_json = json.loads((out_dir / "jcl.json").read_text(encoding="utf-8"))
        self.assertEqual(len(jcl_json["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
