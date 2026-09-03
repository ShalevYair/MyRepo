import unittest

import _pathsetup  # noqa: F401
from natlib import config


class TestLoadRealConfig(unittest.TestCase):
    """Loads the actual pipeline/config.yaml shipped in this repo — the
    single source of truth WORKPLAN.md 0.2 calls for. If someone edits a
    number in config.yaml without reading MERGE-PLAN.md section 5, this
    is what should catch the drift."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load()

    def test_top_level_sections_present(self):
        for key in ("paths", "encoding", "scan", "graveyard_library_patterns",
                    "steplib", "propagation", "llm", "validate"):
            self.assertIn(key, self.cfg)

    def test_forward_weights_match_merge_plan_section_5_2(self):
        w = self.cfg["propagation"]["forward_weights"]
        self.assertEqual(w["CALLNAT"], 0.97)
        self.assertEqual(w["FETCH"], 0.97)
        self.assertEqual(w["USING_MAP"], 0.97)
        self.assertEqual(w["INCLUDE"], 1.00)
        self.assertEqual(w["USING_DATA_AREA"], 1.00)
        self.assertEqual(w["PERFORM_EXTERNAL"], 0.90)
        self.assertEqual(w["CALL3GL"], 0.95)

    def test_dead_weights_match_merge_plan_section_5_4(self):
        w = self.cfg["propagation"]["dead_weights"]
        self.assertEqual(w["shadow_copy"], 0.95)
        self.assertEqual(w["stale_library"], 0.90)
        self.assertEqual(w["graveyard_name"], 0.85)
        self.assertEqual(w["zero_fan_in_old"], 0.70)

    def test_thresholds_match_merge_plan_section_5_5(self):
        t = self.cfg["propagation"]["thresholds"]
        self.assertEqual(t["alive_live"], 0.30)
        self.assertEqual(t["dead_candidate"], 0.85)
        self.assertEqual(t["alive_low"], 0.05)

    def test_solver_settings_match_merge_plan_section_5_5(self):
        s = self.cfg["propagation"]["solver"]
        self.assertEqual(s["max_iterations"], 50)
        self.assertEqual(s["epsilon"], 0.0001)

    def test_llm_model_is_gemini_3_8_flash(self):
        self.assertEqual(self.cfg["llm"]["model"], "gemini-3.8-flash")

    def test_encoding_candidates_match_natlib(self):
        from natlib import encoding
        self.assertEqual(tuple(self.cfg["encoding"]["candidates"]), encoding.ENCODINGS)

    def test_scan_constants_match_app_js(self):
        # app.js: CHUNK = 8 * 1024 * 1024, SNIFF = 4 * 1024 * 1024, record pad 12.
        self.assertEqual(self.cfg["scan"]["chunk_bytes"], 8 * 1024 * 1024)
        self.assertEqual(self.cfg["scan"]["sniff_bytes"], 4 * 1024 * 1024)
        self.assertEqual(self.cfg["scan"]["record_pad"], 12)

    def test_validate_tolerance_is_zero(self):
        # WORKPLAN.md stage 1.3: any mismatch stops the pipeline, not a
        # configurable slack that quietly grows over time.
        self.assertEqual(self.cfg["validate"]["tolerance"], 0)


class TestLoadErrors(unittest.TestCase):
    def test_missing_file_raises_config_error(self):
        with self.assertRaises(config.ConfigError):
            config.load("/nonexistent/path/config.yaml")


if __name__ == "__main__":
    unittest.main()
