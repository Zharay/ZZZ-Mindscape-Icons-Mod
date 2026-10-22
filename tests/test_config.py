import json
import tempfile
import unittest
import warnings
from copy import deepcopy
from pathlib import Path

from config import DEFAULTS, archive_name, load_config, read_json, validate_settings
from support import project


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = project(self.root)

    def test_defaults_and_round_trip(self):
        self.assertEqual(load_config(self.root), DEFAULTS)
        (self.root / "config.json").unlink()
        with warnings.catch_warnings(record=True) as found:
            self.assertEqual(load_config(self.root), DEFAULTS)
        self.assertTrue(found)
        self.assertFalse((self.root / "config.json").exists())

    def test_invalid_json_never_falls_back(self):
        for text in ('{', '{"x": 1, "x": 2}', '{}'):
            with self.subTest(text=text):
                (self.root / "config.json").write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(self.root)

    def test_invalid_values(self):
        cases = (
            ("MOD_NAME", "../oops"), ("MOD_NAME", "CON"), ("MOD_VERSION", "3\n4"),
            ("CUR_AGENT", 2), ("SOURCE_DIRS", []),
            ("SOURCE_DIRS", ["sources/M6", "sources/M6"]),
            ("SOURCE_DIRS", ["sources/M6", "other/M6"]),
            ("CLEAN_EXPORTS", 1), ("SEPARATE_INIS", "false"),
            ("CONTRAST_MULTIPLIER", True), ("SATURATION_MULTIPLIER", float("inf")),
            ("SATURATION_MULTIPLIER", -1), ("FALLBACK_MODE", "unknown"),
            ("CONTRAST_MULTIPLIER", 10 ** 1000),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                settings = deepcopy(DEFAULTS)
                settings[key] = value
                with self.assertRaises(ValueError):
                    validate_settings(settings, self.root)

    def test_safe_export_paths(self):
        for path in (".", "..", str(Path.home()), self.root.anchor, "sources", "sources/M6/out",
                     "masks", ".git/cache", "tests", "config.json", "screenshots/output"):
            with self.subTest(path=path):
                settings = deepcopy(DEFAULTS)
                settings["EXPORT_DIR"] = path
                with self.assertRaises(ValueError):
                    validate_settings(settings, self.root)

    def test_link_cleanup_target(self):
        link = self.root / "linked"
        try:
            link.symlink_to(self.root / "sources", target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable to this account")
        settings = deepcopy(DEFAULTS)
        settings["EXPORT_DIR"] = str(link)
        with self.assertRaises(ValueError):
            validate_settings(settings, self.root)

    def test_source_order_and_zero_color(self):
        settings = deepcopy(DEFAULTS)
        settings["SOURCE_DIRS"].reverse()
        settings["CONTRAST_MULTIPLIER"] = 0
        settings["SATURATION_MULTIPLIER"] = 0
        self.assertEqual(validate_settings(settings, self.root), settings)
        self.assertEqual(archive_name(settings), "mindscape_icons_v3.3.zip")

    def test_unknown_or_missing_field(self):
        for key in ("UNKNOWN", "MOD_NAME"):
            settings = deepcopy(DEFAULTS)
            if key == "UNKNOWN":
                settings[key] = True
            else:
                settings.pop(key)
            with self.assertRaises(ValueError):
                validate_settings(settings, self.root)

    def test_duplicate_agent_json_keys(self):
        path = self.root / "agents.json"
        path.write_text('{"Alice": [], "Alice": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            read_json(path)
