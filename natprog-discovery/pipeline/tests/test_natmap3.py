import json
import pathlib
import tempfile
import unittest

import _pathsetup  # noqa: F401
import natmap3


class TestLeadingKeyword(unittest.TestCase):
    def test_simple_keyword(self):
        self.assertEqual(natmap3.leading_keyword("CALLNAT 'X'"), "CALLNAT")

    def test_keyword_with_hyphen(self):
        self.assertEqual(natmap3.leading_keyword("END-IF"), "END-IF")

    def test_leading_whitespace_stripped(self):
        self.assertEqual(natmap3.leading_keyword("    IF X = Y"), "IF")

    def test_stops_at_paren(self):
        self.assertEqual(natmap3.leading_keyword("IF(X=Y)"), "IF")

    def test_stops_at_period(self):
        self.assertEqual(natmap3.leading_keyword("PROGRAM-ID.SOMETHING"), "PROGRAM-ID")

    def test_empty_line(self):
        self.assertIsNone(natmap3.leading_keyword(""))
        self.assertIsNone(natmap3.leading_keyword("   "))

    def test_line_not_starting_with_letter_is_not_a_keyword(self):
        self.assertIsNone(natmap3.leading_keyword("#VAR := 1"))

    def test_too_long_token_rejected(self):
        self.assertIsNone(natmap3.leading_keyword("A" * 25))


class TestStripInlineComment(unittest.TestCase):
    def test_no_comment_unchanged(self):
        self.assertEqual(natmap3.strip_inline_comment("CALLNAT 'X'"), "CALLNAT 'X'")

    def test_strips_from_first_slash_star(self):
        self.assertEqual(natmap3.strip_inline_comment("CALLNAT 'X' /* note"), "CALLNAT 'X'")

    def test_all_comment_becomes_empty(self):
        self.assertEqual(natmap3.strip_inline_comment("/* just a comment"), "")


class TestParseObjectStatementCounts(unittest.TestCase):
    def test_code_lines_counts_only_exec_keywords(self):
        text = "DEFINE DATA LOCAL\nEND-DEFINE\nMOVE 1 TO #X\nCOMMENT-ISH-BUT-NOT-A-KEYWORD\n"
        r = natmap3.parse_object(text)
        # DEFINE, END-DEFINE, MOVE are exec keywords; the 4th line's leading
        # token isn't in _EXEC_KEYWORDS, so it doesn't count.
        self.assertEqual(r["code_lines"], 3)

    def test_if_decide_compute_counted_separately(self):
        text = "IF #X = 1\nEND-IF\nDECIDE ON FIRST VALUE OF #Y\nEND-DECIDE\nCOMPUTE #Z = 1 + 1\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["if_count"], 1)
        self.assertEqual(r["decide_count"], 1)
        self.assertEqual(r["compute_count"], 1)

    def test_comment_line_not_counted(self):
        text = "* IF THIS WERE REAL IT WOULD COUNT\nMOVE 1 TO #X\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["code_lines"], 1)
        self.assertEqual(r["if_count"], 0)

    def test_blank_line_skipped(self):
        text = "MOVE 1 TO #X\n\n\nMOVE 2 TO #Y\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["code_lines"], 2)


class TestParseObjectNesting(unittest.TestCase):
    def test_balanced_single_level(self):
        text = "IF #X = 1\nMOVE 1 TO #Y\nEND-IF\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["max_depth"], 1)
        self.assertFalse(r["unbalanced"])

    def test_nested_depth_tracked(self):
        text = "IF #A\nFOR #I 1 TO 10\nDECIDE ON FIRST VALUE OF #B\nEND-DECIDE\nEND-FOR\nEND-IF\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["max_depth"], 3)
        self.assertFalse(r["unbalanced"])

    def test_missing_end_is_unbalanced(self):
        text = "IF #X = 1\nMOVE 1 TO #Y\n"  # no END-IF
        r = natmap3.parse_object(text)
        self.assertTrue(r["unbalanced"])

    def test_extra_end_is_unbalanced(self):
        text = "END-IF\nMOVE 1 TO #Y\n"  # closer with no opener
        r = natmap3.parse_object(text)
        self.assertTrue(r["unbalanced"])

    def test_repeat_and_for_tracked(self):
        text = "REPEAT\nEND-REPEAT\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["max_depth"], 1)
        self.assertFalse(r["unbalanced"])


class TestParseObjectEdges(unittest.TestCase):
    def _kinds(self, edges, kind):
        return [e for e in edges if e["kind"] == kind]

    def test_callnat_literal(self):
        r = natmap3.parse_object("CALLNAT 'HICNEWN3'\n")
        e = self._kinds(r["edges"], "CALLNAT")
        self.assertEqual(e, [{"kind": "CALLNAT", "raw_target": "HICNEWN3", "dynamic": False}])

    def test_callnat_dynamic_variable_target(self):
        r = natmap3.parse_object("CALLNAT #PGM-NAME\n")
        e = self._kinds(r["edges"], "CALLNAT")
        self.assertEqual(e, [{"kind": "CALLNAT", "raw_target": "#PGM-NAME", "dynamic": True}])

    def test_fetch_return_and_repeat_forms(self):
        r = natmap3.parse_object("FETCH RETURN 'PROGA'\nFETCH REPEAT 'PROGB'\nFETCH 'PROGC'\n")
        e = self._kinds(r["edges"], "FETCH")
        self.assertEqual([x["raw_target"] for x in e], ["PROGA", "PROGB", "PROGC"])

    def test_perform_not_treated_as_literal_dynamic(self):
        r = natmap3.parse_object("PERFORM MY-SUB\n")
        e = self._kinds(r["edges"], "PERFORM")
        self.assertEqual(e, [{"kind": "PERFORM", "raw_target": "MY-SUB", "dynamic": False}])

    def test_perform_break_is_not_a_target(self):
        # app.js RE.perform has a negative lookahead for PERFORM BREAK.
        r = natmap3.parse_object("PERFORM BREAK\n")
        self.assertEqual(self._kinds(r["edges"], "PERFORM"), [])

    def test_include_copycode(self):
        r = natmap3.parse_object("INCLUDE MYCOPY\n")
        self.assertEqual(self._kinds(r["edges"], "INCLUDE"),
                          [{"kind": "INCLUDE", "raw_target": "MYCOPY", "dynamic": False}])

    def test_using_data_area_each_kind(self):
        text = "LOCAL USING MYLDA\nGLOBAL USING MYGDA\nPARAMETER USING MYPDA\n"
        r = natmap3.parse_object(text)
        e = self._kinds(r["edges"], "USING")
        self.assertEqual([x["raw_target"] for x in e], ["MYLDA", "MYGDA", "MYPDA"])

    def test_using_map_distinct_from_using_data_area(self):
        r = natmap3.parse_object("USING MAP 'MYMAP'\n")
        self.assertEqual(self._kinds(r["edges"], "USING"), [])
        self.assertEqual(self._kinds(r["edges"], "USING_MAP"),
                          [{"kind": "USING_MAP", "raw_target": "MYMAP", "dynamic": False}])

    def test_call3gl_literal_and_dynamic(self):
        r = natmap3.parse_object("CALL 'COBPGM1'\nCALL #DYNPGM\n")
        e = self._kinds(r["edges"], "CALL3GL")
        self.assertEqual(e, [
            {"kind": "CALL3GL", "raw_target": "COBPGM1", "dynamic": False},
            {"kind": "CALL3GL", "raw_target": "#DYNPGM", "dynamic": True},
        ])

    def test_call3gl_not_confused_with_callnat(self):
        # \bCALL\s+' must not match inside CALLNAT -- no whitespace follows
        # "CALL" there, it's immediately "NAT".
        r = natmap3.parse_object("CALLNAT 'HICNEWN3'\n")
        self.assertEqual(self._kinds(r["edges"], "CALL3GL"), [])

    def test_define_subroutine_recorded(self):
        r = natmap3.parse_object("DEFINE SUBROUTINE MY-SUB\nEND-SUBROUTINE\n")
        self.assertEqual(r["subroutines"], {"MY-SUB"})

    def test_comment_line_produces_no_edges(self):
        r = natmap3.parse_object("* CALLNAT 'SHOULDNOTMATCH'\n")
        self.assertEqual(r["edges"], [])

    def test_inline_comment_after_statement_still_matches(self):
        r = natmap3.parse_object("CALLNAT 'REAL1' /* explanatory note\n")
        self.assertEqual(self._kinds(r["edges"], "CALLNAT"),
                          [{"kind": "CALLNAT", "raw_target": "REAL1", "dynamic": False}])


class TestParseObjectDdmAccess(unittest.TestCase):
    def test_read_records_ddm_and_op(self):
        r = natmap3.parse_object("READ MYVIEW BY ISN\n")
        self.assertEqual(r["ddm_access"], [{"ddm": "MYVIEW", "op": "READ"}])

    def test_find_with_record_limit_clause(self):
        r = natmap3.parse_object("FIND (10) MYVIEW WITH FIELD = 'X'\n")
        self.assertEqual(r["ddm_access"], [{"ddm": "MYVIEW", "op": "FIND"}])

    def test_store_records_write(self):
        r = natmap3.parse_object("STORE MYVIEW\n")
        self.assertEqual(r["ddm_access"], [{"ddm": "MYVIEW", "op": "STORE"}])

    def test_bare_update_attributed_to_most_recent_view(self):
        text = "READ MYVIEW BY ISN\nUPDATE\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["ddm_access"], [
            {"ddm": "MYVIEW", "op": "READ"},
            {"ddm": "MYVIEW", "op": "UPDATE"},
        ])

    def test_bare_delete_with_no_prior_view_is_not_recorded(self):
        r = natmap3.parse_object("DELETE\n")
        self.assertEqual(r["ddm_access"], [])

    def test_second_read_changes_current_view_for_subsequent_write(self):
        text = "READ VIEWA BY ISN\nREAD VIEWB BY ISN\nUPDATE\n"
        r = natmap3.parse_object(text)
        self.assertEqual(r["ddm_access"][-1], {"ddm": "VIEWB", "op": "UPDATE"})


class TestResolveTarget(unittest.TestCase):
    def setUp(self):
        # name -> [(object_id, library, type)]
        self.idx = natmap3.build_name_index([
            {"object_id": "RC/HICNEWN3", "library": "RC", "name": "HICNEWN3", "type": "N"},
            {"object_id": "RCOLD/HICNEWN3", "library": "RCOLD", "name": "HICNEWN3", "type": "N"},
            {"object_id": "SYSTEM/UTIL1", "library": "SYSTEM", "name": "UTIL1", "type": "N"},
            {"object_id": "RC/MYSUB", "library": "RC", "name": "MYSUB", "type": "S"},
            {"object_id": "RC/MYSUB", "library": "RC", "name": "MYSUB", "type": "F"},
        ])

    def test_same_library_unique_match(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "RC", "HICNEWN3", [], "SYSTEM")
        self.assertEqual((oid, scope, cands), ("RC/HICNEWN3", "same_library", []))

    def test_no_same_library_falls_through_to_system(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "OTHERLIB", "UTIL1", [], "SYSTEM")
        self.assertEqual((oid, scope, cands), ("SYSTEM/UTIL1", "system", []))

    def test_steplib_chain_used_before_system(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "OTHERLIB", "HICNEWN3", ["RCOLD"], "SYSTEM")
        self.assertEqual((oid, scope, cands), ("RCOLD/HICNEWN3", "steplib", []))

    def test_ambiguous_across_libraries_lists_all_candidates(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "OTHERLIB", "HICNEWN3", [], "SYSTEM")
        self.assertIsNone(oid)
        self.assertEqual(scope, "ambiguous")
        self.assertEqual(set(cands), {"RC/HICNEWN3", "RCOLD/HICNEWN3"})

    def test_unresolved_when_nothing_matches_name(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "RC", "NOWHERE", [], "SYSTEM")
        self.assertEqual((oid, scope, cands), (None, "unresolved", []))

    def test_require_type_filters_candidates(self):
        oid, scope, cands = natmap3.resolve_target(self.idx, "RC", "MYSUB", [], "SYSTEM", require_type="S")
        self.assertEqual((oid, scope, cands), ("RC/MYSUB", "same_library", []))


class TestLoadCobolBridge(unittest.TestCase):
    def test_loads_and_indexes_by_object_and_upper_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "cobol.json"
            p.write_text(json.dumps({"natural_bridge": [
                {"cobol_program": "HELPER1", "natural_call_target": "Helper1",
                 "natural_object_id": "RC/CALLER", "natural_source_path": "source/RC/CALLER.nat"},
            ]}), encoding="utf-8")
            bridge = natmap3.load_cobol_bridge(p)
            self.assertEqual(bridge, {("RC/CALLER", "HELPER1"): "HELPER1"})

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(natmap3.load_cobol_bridge(pathlib.Path("/nope/cobol.json")), {})

    def test_none_path_returns_empty_dict(self):
        self.assertEqual(natmap3.load_cobol_bridge(None), {})


class TestResolveEdges(unittest.TestCase):
    def test_internal_perform_target_dropped_not_emitted(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {
            "subroutines": {"MY-SUB"}, "ddm_access": [],
            "edges": [{"kind": "PERFORM", "raw_target": "MY-SUB", "dynamic": False}],
        }}
        calls = natmap3.resolve_edges(objects, parsed, [], "SYSTEM", {})
        self.assertEqual(calls, [])

    def test_external_perform_target_resolved(self):
        objects = [
            {"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"},
            {"object_id": "RC/SUBX", "library": "RC", "name": "SUBX", "type": "S"},
        ]
        parsed = {"RC/A": {
            "subroutines": set(), "ddm_access": [],
            "edges": [{"kind": "PERFORM", "raw_target": "SUBX", "dynamic": False}],
        }}
        calls = natmap3.resolve_edges(objects, parsed, [], "SYSTEM", {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resolved_to"], "RC/SUBX")
        self.assertEqual(calls[0]["scope"], "same_library")

    def test_dynamic_edge_emitted_as_unresolved_not_dropped(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {
            "subroutines": set(), "ddm_access": [],
            "edges": [{"kind": "CALLNAT", "raw_target": "#VAR", "dynamic": True}],
        }}
        calls = natmap3.resolve_edges(objects, parsed, [], "SYSTEM", {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["scope"], "unresolved")
        self.assertTrue(calls[0]["dynamic"])
        self.assertIsNone(calls[0]["resolved_to"])

    def test_call3gl_always_external_3gl_scope(self):
        objects = [
            {"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"},
            {"object_id": "RC/B", "library": "RC", "name": "COBPGM1", "type": "N"},
        ]
        parsed = {"RC/A": {
            "subroutines": set(), "ddm_access": [],
            "edges": [{"kind": "CALL3GL", "raw_target": "COBPGM1", "dynamic": False}],
        }}
        calls = natmap3.resolve_edges(objects, parsed, [], "SYSTEM", {})
        self.assertEqual(calls[0]["scope"], "external_3gl")
        self.assertIsNone(calls[0]["resolved_to"])  # no Natural object is a valid CALL3GL target

    def test_call3gl_resolved_via_cobol_bridge(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {
            "subroutines": set(), "ddm_access": [],
            "edges": [{"kind": "CALL3GL", "raw_target": "HELPER1", "dynamic": False}],
        }}
        bridge = {("RC/A", "HELPER1"): "HELPER1"}
        calls = natmap3.resolve_edges(objects, parsed, [], "SYSTEM", bridge)
        self.assertEqual(calls[0]["scope"], "external_3gl")
        self.assertEqual(calls[0]["resolved_to"], "COBOL:HELPER1")


class TestComputeFamilies(unittest.TestCase):
    def test_singleton_hash_has_no_family(self):
        objects = [{"object_id": "RC/A", "sha256_norm": "h1"}]
        family_of, dup_pairs = natmap3.compute_families(objects)
        self.assertEqual(family_of, {})
        self.assertEqual(dup_pairs, [])

    def test_two_members_get_family_and_one_pair(self):
        objects = [
            {"object_id": "RC/A", "sha256_norm": "h1"},
            {"object_id": "RCOLD/A", "sha256_norm": "h1"},
        ]
        family_of, dup_pairs = natmap3.compute_families(objects)
        self.assertEqual(family_of, {"RC/A": "h1", "RCOLD/A": "h1"})
        self.assertEqual(dup_pairs, [{"a": "RC/A", "b": "RCOLD/A"}])

    def test_three_members_get_three_pairs(self):
        objects = [
            {"object_id": "A", "sha256_norm": "h1"},
            {"object_id": "B", "sha256_norm": "h1"},
            {"object_id": "C", "sha256_norm": "h1"},
        ]
        _, dup_pairs = natmap3.compute_families(objects)
        self.assertEqual(len(dup_pairs), 3)


class TestBuildNatmap(unittest.TestCase):
    def test_fan_in_and_fan_out(self):
        objects = [
            {"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"},
            {"object_id": "RC/B", "library": "RC", "name": "B", "type": "N"},
        ]
        parsed = {oid: {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                         "max_depth": 0, "unbalanced": False, "ddm_access": []}
                  for oid in ("RC/A", "RC/B")}
        calls = [{"from": "RC/A", "kind": "CALLNAT", "target": "B", "resolved_to": "RC/B",
                   "scope": "same_library", "candidates": [], "dynamic": False}]
        nm = natmap3.build_natmap(objects, parsed, calls)
        by_id = {o["object_id"]: o for o in nm["objects"]}
        self.assertEqual(by_id["RC/A"]["fan_out"], 1)
        self.assertEqual(by_id["RC/B"]["fan_in"], 1)
        self.assertEqual(by_id["RC/A"]["fan_in"], 0)

    def test_unresolved_edges_do_not_count_toward_fan(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                            "max_depth": 0, "unbalanced": False, "ddm_access": []}}
        calls = [{"from": "RC/A", "kind": "CALLNAT", "target": "X", "resolved_to": None,
                   "scope": "unresolved", "candidates": [], "dynamic": False}]
        nm = natmap3.build_natmap(objects, parsed, calls)
        self.assertEqual(nm["objects"][0]["fan_out"], 0)

    def test_ui_class_online_for_map_batch_otherwise(self):
        objects = [
            {"object_id": "RC/M1", "library": "RC", "name": "M1", "type": "M"},
            {"object_id": "RC/F1", "library": "RC", "name": "F1", "type": "F"},
        ]
        parsed = {oid: {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                         "max_depth": 0, "unbalanced": False, "ddm_access": []}
                  for oid in ("RC/M1", "RC/F1")}
        nm = natmap3.build_natmap(objects, parsed, [])
        by_id = {o["object_id"]: o for o in nm["objects"]}
        self.assertEqual(by_id["RC/M1"]["ui_class"], "online")
        self.assertEqual(by_id["RC/F1"]["ui_class"], "batch")

    def test_writes_true_when_any_write_op_present(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                            "max_depth": 0, "unbalanced": False,
                            "ddm_access": [{"ddm": "VIEWA", "op": "READ"}, {"ddm": "VIEWA", "op": "UPDATE"}]}}
        nm = natmap3.build_natmap(objects, parsed, [])
        self.assertTrue(nm["objects"][0]["writes"])
        self.assertEqual(nm["ddm_access"], [
            {"object_id": "RC/A", "ddm": "VIEWA", "op": "READ"},
            {"object_id": "RC/A", "ddm": "VIEWA", "op": "UPDATE"},
        ])

    def test_primary_ddm_is_most_accessed(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                            "max_depth": 0, "unbalanced": False,
                            "ddm_access": [{"ddm": "VIEWA", "op": "READ"},
                                           {"ddm": "VIEWB", "op": "READ"},
                                           {"ddm": "VIEWA", "op": "READ"}]}}
        nm = natmap3.build_natmap(objects, parsed, [])
        self.assertEqual(nm["objects"][0]["primary_ddm"], "VIEWA")

    def test_placeholder_fields_present_but_neutral(self):
        # domain/self_redundancy/n_obsolete are NOT computed (see module
        # docstring) -- they must still exist so natural-viewer.html's
        # `o.field || default` reads see a neutral value, not an exception.
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                            "max_depth": 0, "unbalanced": False, "ddm_access": []}}
        nm = natmap3.build_natmap(objects, parsed, [])
        o = nm["objects"][0]
        self.assertIn("domain", o)
        self.assertIn("self_redundancy", o)
        self.assertIn("n_obsolete", o)
        self.assertEqual(o["self_redundancy"], 0.0)
        self.assertEqual(o["n_obsolete"], 0)

    def test_dynamic_callnat_ratio_computed(self):
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N"}]
        parsed = {"RC/A": {"code_lines": 0, "if_count": 0, "decide_count": 0, "compute_count": 0,
                            "max_depth": 0, "unbalanced": False, "ddm_access": []}}
        calls = [
            {"from": "RC/A", "kind": "CALLNAT", "target": "X", "resolved_to": "RC/X",
             "scope": "same_library", "candidates": [], "dynamic": False},
            {"from": "RC/A", "kind": "CALLNAT", "target": "#VAR", "resolved_to": None,
             "scope": "unresolved", "candidates": [], "dynamic": True},
        ]
        nm = natmap3.build_natmap(objects, parsed, calls)
        self.assertEqual(nm["meta"]["dynamic_callnat_ratio"], 0.5)

    def test_zero_callnat_ratio_is_zero_not_a_crash(self):
        nm = natmap3.build_natmap([], {}, [])
        self.assertEqual(nm["meta"]["dynamic_callnat_ratio"], 0.0)
        self.assertEqual(nm["meta"]["dynamic_call3gl_ratio"], 0.0)


class TestMainCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    def _write_source(self, out_dir, rel, text):
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def _run(self, extra_args):
        import contextlib
        stdout_path = self.tmp_path / "stdout.json"
        with stdout_path.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
            rc = natmap3.main(extra_args)
        summary = json.loads(stdout_path.read_text(encoding="utf-8")) if stdout_path.stat().st_size else None
        return rc, summary

    def test_end_to_end_writes_natmap_json(self):
        out_dir = self.tmp_path / "out"
        self._write_source(out_dir, "source/RC/A.nat", "CALLNAT 'B'\n")
        self._write_source(out_dir, "source/RC/B.nat", "MOVE 1 TO #X\n")
        objects = [
            {"object_id": "RC/A", "library": "RC", "name": "A", "type": "N",
             "type_meaning": "Subprogram", "source_path": "source/RC/A.nat", "sha256_norm": "h1"},
            {"object_id": "RC/B", "library": "RC", "name": "B", "type": "N",
             "type_meaning": "Subprogram", "source_path": "source/RC/B.nat", "sha256_norm": "h2"},
        ]
        with (out_dir / "objects.jsonl").open("w", encoding="utf-8") as fh:
            for o in objects:
                fh.write(json.dumps(o) + "\n")

        rc, summary = self._run(["--out-dir", str(out_dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(summary["object_count"], 2)
        self.assertEqual(summary["total_edges"], 1)

        natmap = json.loads((out_dir / "natmap.json").read_text(encoding="utf-8"))
        self.assertEqual(natmap["meta"]["object_count"], 2)
        self.assertEqual(natmap["calls"][0]["resolved_to"], "RC/B")
        by_id = {o["object_id"]: o for o in natmap["objects"]}
        self.assertEqual(by_id["RC/A"]["fan_out"], 1)
        self.assertEqual(by_id["RC/B"]["fan_in"], 1)

    def test_missing_objects_jsonl_errors_cleanly(self):
        out_dir = self.tmp_path / "out"
        out_dir.mkdir()
        rc, _ = self._run(["--out-dir", str(out_dir)])
        self.assertEqual(rc, 1)

    def test_missing_individual_source_file_recorded_not_fatal(self):
        out_dir = self.tmp_path / "out"
        objects = [{"object_id": "RC/GONE", "library": "RC", "name": "GONE", "type": "N",
                    "type_meaning": "Subprogram", "source_path": "source/RC/GONE.nat", "sha256_norm": "h1"}]
        out_dir.mkdir()
        with (out_dir / "objects.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(objects[0]) + "\n")
        rc, summary = self._run(["--out-dir", str(out_dir)])
        self.assertEqual(rc, 0)
        self.assertEqual(summary["read_errors"][0]["object_id"], "RC/GONE")

    def test_cobol_json_auto_detected_from_out_dir(self):
        out_dir = self.tmp_path / "out"
        self._write_source(out_dir, "source/RC/A.nat", "CALL 'HELPER1'\n")
        objects = [{"object_id": "RC/A", "library": "RC", "name": "A", "type": "N",
                    "type_meaning": "Subprogram", "source_path": "source/RC/A.nat", "sha256_norm": "h1"}]
        with (out_dir / "objects.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(objects[0]) + "\n")
        (out_dir / "cobol.json").write_text(json.dumps({"natural_bridge": [
            {"cobol_program": "HELPER1", "natural_call_target": "HELPER1",
             "natural_object_id": "RC/A", "natural_source_path": "source/RC/A.nat"},
        ]}), encoding="utf-8")

        rc, summary = self._run(["--out-dir", str(out_dir)])
        self.assertEqual(rc, 0)
        self.assertTrue(summary["cobol_bridge_loaded"])
        natmap = json.loads((out_dir / "natmap.json").read_text(encoding="utf-8"))
        self.assertEqual(natmap["calls"][0]["resolved_to"], "COBOL:HELPER1")


if __name__ == "__main__":
    unittest.main()
