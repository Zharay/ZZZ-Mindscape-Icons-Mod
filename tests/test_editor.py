import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from editor_model import (
    Draft, duplicate_hashes, hash_from_filename, validate_mask,
    validate_transform_image,
)
from support import entry, project


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project(self.root)
        self.draft = Draft(self.root)

    def artwork(self, color="red"):
        path = self.root / "import.png"
        Image.new("RGBA", (12, 14), color).save(path)
        return path

    def test_noop_save_keeps_exact_bytes_and_no_backups(self):
        original = (self.root / "agents.json").read_bytes()
        self.assertFalse(self.draft.dirty)
        self.assertEqual(self.draft.save(), [])
        self.assertEqual((self.root / "agents.json").read_bytes(), original)
        self.assertEqual(list(self.root.rglob("*.bak")), [])

    def test_agent_skin_mask_round_trip(self):
        self.draft.add_agent("NewAgent")
        index = self.draft.add_skin("NewAgent")
        self.assertEqual(index, 1)
        self.draft.stage_image("sources/M6", "NewAgent", index, self.artwork())
        self.draft.apply_mask("NewAgent", index, "round", entry(), "sources/M6")
        self.draft.agents["Alice"][0].pop("tab")
        self.draft.save()
        loaded = Draft(self.root)
        self.assertEqual(loaded.agents, self.draft.agents)
        self.assertEqual(loaded.agents["NewAgent"][0], {})
        self.assertEqual(loaded.agents["NewAgent"][1]["round"], entry())
        self.assertNotIn("tab", loaded.agents["Alice"][0])
        self.assertTrue((self.root / "sources" / "M6" / "NewAgentSkinM6.png").exists())
        self.assertFalse(loaded.dirty)

    def test_invalid_agents_and_skin_collision(self):
        for name in ("alice", "../Oops", "CON", "Bad Name", "1Wrong", "AliceSkin"):
            with self.subTest(name=name):
                if name == "AliceSkin":
                    self.draft.add_skin("Alice")
                with self.assertRaises(ValueError):
                    self.draft.add_agent(name)

    def test_unknown_cur_agent(self):
        settings = deepcopy(self.draft.settings)
        settings["CUR_AGENT"] = "Missing"
        with self.assertRaisesRegex(ValueError, "CUR_AGENT"):
            self.draft.set_settings(settings)

    def test_validation_rejects_invalid_transform(self):
        for key, value in (
            ("hash", "abcd"), ("hash", "g1234567"), ("offset", [1.1, 0]),
            ("scale", 0), ("scale", True), ("scale", float("nan")),
            ("rotation", float("inf")), ("scale", 10 ** 1000),
            ("offset", [2 ** 40, 0]),
        ):
            with self.subTest(key=key, value=value):
                data = entry()
                data[key] = value
                with self.assertRaises(ValueError):
                    validate_mask(data)
        for scale in (1e-8, 1e9, 1e308):
            data = entry()
            data["scale"] = scale
            with self.assertRaises(ValueError):
                validate_transform_image(data, Image.new("RGBA", (10, 10)))

    def test_hash_filename(self):
        self.assertEqual(hash_from_filename("texture-9ec5D7fe-ps-t0.dds"), "9ec5d7fe")
        for filename in ("photo.png", "1234abcd-2345bcde.dds", "asset1234abcd.dds", "01234abcd.dds"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                hash_from_filename(filename)

    def test_shared_transform_and_duplicate_warnings(self):
        self.draft.apply_mask("Alice", 0, "select", entry(), "sources/M6")
        self.draft.apply_mask("Alice", 0, "tab", entry(), "sources/M3")
        self.assertEqual(self.draft.agents["Alice"][0]["select"], self.draft.agents["Alice"][0]["tab"])
        self.assertEqual(len(duplicate_hashes(self.draft.agents)), 1)

    def test_import_dedup_discard_and_transparency(self):
        base = self.root / "sources" / "M6" / "Alice.png"
        self.assertIsNone(self.draft.stage_image("sources/M6", "Alice", 0, base))
        self.assertFalse(self.draft.dirty)
        target = self.draft.stage_image("sources/M6", "Alice", 0, self.artwork((1, 2, 3, 40)))
        self.assertFalse(target.exists())
        self.draft.save()
        with Image.open(target) as image:
            self.assertEqual(image.getpixel((0, 0)), (1, 2, 3, 40))
        self.draft.stage_image("sources/M6", "Alice", 0, self.artwork("blue"))
        self.draft.discard_imports()
        self.assertFalse(self.draft.dirty)

    def test_config_and_import_saved_together(self):
        settings = deepcopy(self.draft.settings)
        settings["SOURCE_DIRS"].reverse()
        settings["MOD_VERSION"] = "4.0"
        settings["SATURATION_MULTIPLIER"] = 0
        self.draft.set_settings(settings)
        self.draft.stage_image("sources/M3", "Alice", 0, self.artwork())
        backups = self.draft.save()
        self.assertTrue(backups)
        self.assertEqual(Draft(self.root).settings, settings)

    def test_config_cannot_orphan_import(self):
        self.draft.stage_image("sources/M6", "Alice", 0, self.artwork())
        settings = deepcopy(self.draft.settings)
        settings["SOURCE_DIRS"] = ["sources/M3"]
        with self.assertRaisesRegex(ValueError, "staged imports"):
            self.draft.set_settings(settings)
        self.assertEqual(self.draft.settings["SOURCE_DIRS"], ["sources/M6", "sources/M3"])

    def test_stale_json_and_artwork(self):
        for kind in ("json", "art"):
            with self.subTest(kind=kind):
                draft = Draft(self.root)
                if kind == "json":
                    path = self.root / "agents.json"
                    original = path.read_bytes()
                    path.write_bytes(original + b"\n")
                else:
                    path = draft.target("sources/M6", "Alice", 0)
                    draft.stage_image("sources/M6", "Alice", 0, self.artwork())
                    Image.new("RGBA", (8, 9)).save(path)
                with self.assertRaisesRegex(ValueError, "outside"):
                    draft.save()

    def test_failure_during_save_restores_previous_files(self):
        self.draft.add_agent("New")
        self.draft.stage_image("sources/M6", "New", 0, self.artwork())
        original = (self.root / "agents.json").read_bytes()
        replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Injected replacement failure")
            return replace(source, target)

        with patch("editor_model.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "Previous files restored"):
                self.draft.save()
        self.assertEqual((self.root / "agents.json").read_bytes(), original)
        self.assertFalse(self.draft.target("sources/M6", "New", 0).exists())
        self.assertTrue(self.draft.dirty)
        self.assertEqual(list(self.root.rglob(".editor-*")), [])
        self.draft.save()
        self.assertFalse(self.draft.dirty)

    def test_preflight_backup_failure_changes_nothing(self):
        self.draft.add_agent("New")
        original = (self.root / "agents.json").read_bytes()
        with patch("editor_model.shutil.copy2", side_effect=OSError("No permission")):
            with self.assertRaisesRegex(ValueError, "Save failed"):
                self.draft.save()
        self.assertEqual((self.root / "agents.json").read_bytes(), original)
        self.assertEqual(list(self.root.rglob(".editor-*")), [])

    def test_rollback_failure_reports_recovery_location(self):
        self.draft.add_agent("New")
        settings = deepcopy(self.draft.settings)
        settings["MOD_VERSION"] = "5"
        self.draft.set_settings(settings)
        replace = os.replace
        calls = 0

        def fail_after_first(source, target):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("Device failure")
            return replace(source, target)

        with patch("editor_model.os.replace", side_effect=fail_after_first):
            with self.assertRaisesRegex(ValueError, r"Recovery needed:[\s\S]*backup:.*\.bak"):
                self.draft.save()

    def test_invalid_image_is_not_staged(self):
        path = self.root / "bad.png"
        path.write_text("not an image")
        with self.assertRaises(OSError):
            self.draft.stage_image("sources/M6", "Alice", 0, path)
        self.assertFalse(self.draft.imports)

    def test_source_directory_link_preserves_configured_variant_name(self):
        alias = self.root / "sources" / "Alias"
        try:
            alias.symlink_to(self.root / "sources" / "M6", target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable to this account")
        settings = deepcopy(self.draft.settings)
        settings["SOURCE_DIRS"] = ["sources/Alias"]
        self.draft.set_settings(settings)
        target = self.draft.stage_image("sources/Alias", "Alice", 0, self.artwork())
        self.assertEqual(target.name, "AliceAlias.png")
        self.draft.save()
        image, resolved = self.draft.artwork("sources/Alias", "Alice", 0)
        self.assertEqual(resolved, target)
        self.assertEqual(image.size, (12, 14))
