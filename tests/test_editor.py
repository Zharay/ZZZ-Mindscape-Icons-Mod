import json
import os
import tempfile
import tkinter as tk
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from tkinter import ttk

from PIL import Image

from editor import Editor
from editor_model import (
    BLANK_HASH, Draft, duplicate_hashes, hash_from_filename, validate_mask,
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

    def test_load_replaces_invalid_hashes_and_saves_repairs(self):
        path = self.root / "agents.json"
        agents = {
            "Alice": [
                {"select": entry("bad"), "tab": entry(None), "round": entry("ABCDEF12")},
                {"select": entry(12345678)},
                {"round": entry("gggggggg")},
            ],
            "Bob": [{"tab": entry("")}],
        }
        path.write_text(json.dumps(agents), encoding="utf-8")
        original = path.read_bytes()
        draft = Draft(self.root)
        self.assertEqual(draft.hash_warnings, [
            "Alice / base: select, tab", "Alice / skin1: select",
            "Alice / skin2: round", "Bob / base: tab",
        ])
        self.assertEqual(draft.agents["Alice"][0]["round"]["hash"], "ABCDEF12")
        for name, index, kind in (
            ("Alice", 0, "select"), ("Alice", 0, "tab"), ("Alice", 1, "select"),
            ("Alice", 2, "round"), ("Bob", 0, "tab"),
        ):
            self.assertEqual(draft.agents[name][index][kind]["hash"], BLANK_HASH)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(draft.dirty)
        draft.hash_warnings.clear()
        self.assertTrue(draft.dirty)
        backups = draft.save()
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(json.loads(path.read_bytes()), draft.agents)
        loaded = Draft(self.root)
        self.assertEqual(loaded.hash_warnings, [])
        self.assertFalse(loaded.dirty)

    def test_load_sorts_agents_and_save_repairs_json_order(self):
        path = self.root / "agents.json"
        path.write_text(
            json.dumps({"Zoe": [{}], "alice": [{}], "Bob": [{}]}),
            encoding="utf-8",
        )
        original = path.read_bytes()

        draft = Draft(self.root)
        self.assertEqual(list(draft.agents), ["alice", "Bob", "Zoe"])
        self.assertTrue(draft.dirty)
        backups = draft.save()

        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(list(json.loads(path.read_text(encoding="utf-8"))), ["alice", "Bob", "Zoe"])
        self.assertFalse(draft.dirty)
        self.assertEqual(draft.save(), [])

    def test_save_replaces_invalid_hashes_and_preserves_other_edits(self):
        self.draft.agents["Alice"][0]["select"]["hash"] = "not hex"
        self.draft.agents["Alice"][0]["tab"]["hash"] = ""
        self.draft.agents["Alice"][0]["round"]["offset"] = [10, -20]
        self.draft.save()
        self.assertEqual(self.draft.hash_warnings, ["Alice / base: select, tab"])
        loaded = Draft(self.root)
        self.assertEqual(loaded.agents["Alice"][0]["select"]["hash"], BLANK_HASH)
        self.assertEqual(loaded.agents["Alice"][0]["tab"]["hash"], BLANK_HASH)
        self.assertEqual(loaded.agents["Alice"][0]["round"]["offset"], [10, -20])
        self.assertFalse(self.draft.dirty)

    def test_save_spaces_inline_mask_objects(self):
        self.draft.agents["Alice"][0]["select"]["hash"] = "89abcdef"

        self.draft.save()

        saved = (self.root / "agents.json").read_text(encoding="utf-8")
        self.assertIn(
            '      "select": { "hash": "89abcdef", "offset": [0, 0], '
            '"scale": 0.7, "rotation": 0 }',
            saved,
        )

    def test_hash_repair_does_not_hide_structural_errors(self):
        path = self.root / "agents.json"
        for field, value in (("offset", [1.5, 0]), ("scale", 0), ("rotation", "bad")):
            with self.subTest(field=field):
                invalid = entry("bad")
                invalid[field] = value
                path.write_text(json.dumps({"Alice": [{"select": invalid}]}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, field):
                    Draft(self.root)
        self.draft.agents["Alice"][0]["select"]["hash"] = "bad"
        self.draft.agents["Alice"][0]["select"]["scale"] = 0
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "scale"):
            self.draft.save()
        self.assertEqual(path.read_bytes(), original)

    def test_apply_invalid_hash_uses_placeholder_without_mutating_input(self):
        data = entry("invalid")
        self.draft.apply_mask("Alice", 0, "select", data, "sources/M6")
        self.assertEqual(data["hash"], "invalid")
        self.assertEqual(self.draft.agents["Alice"][0]["select"]["hash"], BLANK_HASH)
        self.assertEqual(self.draft.hash_warnings, ["Alice / base: select"])
        self.draft.save()
        self.assertEqual(Draft(self.root).agents, self.draft.agents)

    def test_apply_still_rejects_missing_fields(self):
        data = entry()
        del data["hash"]
        with self.assertRaisesRegex(ValueError, "expected hash"):
            self.draft.apply_mask("Alice", 0, "select", data, "sources/M6")
        self.assertEqual(self.draft.hash_warnings, [])

    def test_failed_hash_repair_save_can_be_retried(self):
        path = self.root / "agents.json"
        original = path.read_bytes()
        self.draft.agents["Alice"][0]["select"]["hash"] = "invalid"
        with patch("editor_model.shutil.copy2", side_effect=OSError("No permission")):
            with self.assertRaisesRegex(ValueError, "Save failed"):
                self.draft.save()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(self.draft.dirty)
        self.draft.hash_warnings.clear()
        self.draft.save()
        self.assertFalse(self.draft.dirty)
        self.assertEqual(Draft(self.root).agents["Alice"][0]["select"]["hash"], BLANK_HASH)

    def test_blank_placeholders_do_not_trigger_duplicate_warnings(self):
        self.draft.agents["Alice"][0]["select"]["hash"] = BLANK_HASH
        self.draft.agents["Alice"][0]["tab"]["hash"] = BLANK_HASH
        self.assertEqual(duplicate_hashes(self.draft.agents), [])

    def editor(self):
        editor = Editor.__new__(Editor)
        editor.draft = self.draft
        editor.window = Mock()
        editor.append_log = Mock()
        editor.hash_value = Mock()
        editor.hash_value.get.return_value = "invalid"
        editor.variables = {}
        for key, value in (("x", "0"), ("y", "0"), ("scale", "0.7"), ("rotation", "0")):
            editor.variables[key] = Mock()
            editor.variables[key].get.return_value = value
        return editor

    def test_editor_apply_warns_and_updates_invalid_hash_field(self):
        editor = self.editor()
        editor.running = False
        editor.current = ("Alice", 0, "select")
        editor.variant = Mock()
        editor.variant.get.return_value = "sources/M6"
        editor.refresh_labels = Mock()
        editor.status = Mock()
        with patch("editor.messagebox.showwarning") as warning:
            self.assertTrue(editor.apply())
        self.assertIn("Alice / base: select", warning.call_args.args[1])
        editor.hash_value.set.assert_called_once_with(BLANK_HASH)
        self.assertEqual(self.draft.hash_warnings, [])

    def test_editor_save_warns_but_continues(self):
        editor = self.editor()
        editor.running = False
        editor.resolve_fields = Mock(return_value=True)
        editor.status = Mock()
        editor.refresh_labels = Mock()
        self.draft.agents["Alice"][0]["tab"]["hash"] = "bad"
        with patch("editor.messagebox.showwarning") as warning:
            self.assertTrue(editor.save())
        self.assertIn("Alice / base: tab", warning.call_args.args[1])
        self.assertIn(BLANK_HASH, warning.call_args.args[1])
        self.assertEqual(Draft(self.root).agents["Alice"][0]["tab"]["hash"], BLANK_HASH)

    def tree_editor(self):
        editor = self.editor()
        window = tk.Tk()
        window.withdraw()
        self.addCleanup(window.destroy)
        editor.tree = ttk.Treeview(window)
        editor.tree_items = {}
        editor.running = False
        editor.current = None
        editor.search = Mock()
        editor.search.get.return_value = ""
        editor.update_title = Mock()
        return editor

    def tree_tags(self, editor):
        return {
            (editor.tree.item(item, "text").removesuffix(" *"), editor.tree_items[item]):
                editor.tree.item(item, "tags")
            for item in editor.tree_items
        }

    def test_tree_highlights_invalid_masks_and_only_their_ancestors(self):
        self.draft.add_skin("Alice")
        self.draft.add_skin("Alice")
        self.draft.add_agent("Bob")
        self.draft.agents["Alice"][1] = {"select": entry(BLANK_HASH), "tab": entry()}
        self.draft.agents["Alice"][2] = {"round": entry("invalid")}
        editor = self.tree_editor()
        editor.rebuild_tree()
        tagged = {
            identity for identity, tags in self.tree_tags(editor).items()
            if "invalid_hash" in tags
        }
        self.assertEqual(tagged, {
            ("Alice", ("Alice", 0, None)),
            ("Skin", ("Alice", 1, None)),
            ("select", ("Alice", 1, "select")),
            ("Skin2", ("Alice", 2, None)),
            ("round", ("Alice", 2, "round")),
        })
        editor.search.get.return_value = "Bob"
        editor.rebuild_tree()
        self.assertTrue(all(not tags for tags in self.tree_tags(editor).values()))

    def test_tree_displays_agents_alphabetically(self):
        self.draft.agents = {"Zoe": [{}], "alice": [{}], "Bob": [{}]}
        editor = self.tree_editor()
        editor.rebuild_tree()

        roots = editor.tree.get_children()
        self.assertEqual([editor.tree.item(item, "text") for item in roots], ["alice", "Bob", "Zoe"])

    def test_tree_highlights_update_after_repairs_removal_and_save(self):
        self.draft.agents["Alice"][0]["select"]["hash"] = BLANK_HASH
        self.draft.agents["Alice"][0]["tab"]["hash"] = BLANK_HASH
        editor = self.tree_editor()
        editor.rebuild_tree()
        self.assertEqual(sum(bool(tags) for tags in self.tree_tags(editor).values()), 4)
        self.draft.apply_mask("Alice", 0, "select", entry(), "sources/M6")
        editor.refresh_labels()
        self.assertEqual(sum(bool(tags) for tags in self.tree_tags(editor).values()), 3)
        self.draft.save()
        editor.refresh_labels()
        self.assertEqual(sum(bool(tags) for tags in self.tree_tags(editor).values()), 3)
        self.draft.agents["Alice"][0].pop("tab")
        editor.refresh_labels()
        self.assertTrue(all(not tags for tags in self.tree_tags(editor).values()))

    def test_tree_highlights_load_time_hash_replacements(self):
        path = self.root / "agents.json"
        agents = json.loads(path.read_text(encoding="utf-8"))
        agents["Alice"][0]["round"]["hash"] = "invalid"
        path.write_text(json.dumps(agents), encoding="utf-8")
        editor = self.tree_editor()
        editor.draft = Draft(self.root)
        editor.rebuild_tree()
        self.assertEqual(sum(bool(tags) for tags in self.tree_tags(editor).values()), 3)

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

    def test_add_agent_keeps_draft_and_saved_json_alphabetical(self):
        self.draft.add_agent("Aaron")

        self.assertEqual(list(self.draft.agents), ["Aaron", "Alice"])
        self.draft.save()
        saved = json.loads((self.root / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(list(saved), ["Aaron", "Alice"])

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

    def test_import_can_flip_vertically_before_saving(self):
        path = self.root / "vertical.png"
        image = Image.new("RGBA", (2, 2))
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.putpixel((0, 1), (0, 0, 255, 255))
        image.save(path)
        target = self.draft.stage_image("sources/M6", "Alice", 0, path, flip_vertical=True)
        staged = self.draft.imports[target]
        self.assertEqual(staged.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(staged.getpixel((0, 1)), (255, 0, 0, 255))
        self.draft.save()
        with Image.open(target) as saved:
            self.assertEqual(saved.getpixel((0, 0)), (0, 0, 255, 255))
            self.assertEqual(saved.getpixel((0, 1)), (255, 0, 0, 255))

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
