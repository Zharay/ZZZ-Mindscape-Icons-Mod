"""Headless editor drafts, validation, imports, and recoverable persistence."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from typing import TypedDict, cast

from PIL import Image

from config import (
    ROOT, Settings, finite_number, load_config, read_json, resolve_path, safe_component,
    validate_settings,
)
from textures import MASK_SPECS, MaskKind, resolve_source, source_stem


class MaskEntry(TypedDict):
    hash: str
    offset: list[int]
    scale: float
    rotation: float


Agents = dict[str, list[dict[str, MaskEntry]]]
DEFAULT_MASK: MaskEntry = {"hash": "", "offset": [0, 0], "scale": 1.0, "rotation": 0.0}
BLANK_HASH = "00000000"


def valid_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}", value) is not None


def hash_warning(name: str, index: int, kinds: list[str]) -> str:
    skin = "base" if index == 0 else f"skin{index}"
    return f"{name} / {skin}: {', '.join(kinds)}"


def replace_invalid_hashes(value: object) -> list[str]:
    warnings: list[str] = []
    if not isinstance(value, dict):
        return warnings
    for name, skins in value.items():
        if not isinstance(skins, list):
            continue
        for index, skin in enumerate(skins):
            if not isinstance(skin, dict):
                continue
            invalid: list[str] = []
            for kind, entry in skin.items():
                if kind in MASK_SPECS and isinstance(entry, dict) and "hash" in entry:
                    if not valid_hash(entry["hash"]):
                        entry["hash"] = BLANK_HASH
                        invalid.append(kind)
            if invalid:
                warnings.append(hash_warning(name, index, invalid))
    return warnings


def validate_mask(value: object, label: str = "Mask") -> MaskEntry:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_MASK):
        raise ValueError(f"{label}: expected hash, offset, scale, and rotation.")
    if not valid_hash(value["hash"]):
        raise ValueError(f"{label}: game hash must contain exactly eight hexadecimal digits.")
    offset = value["offset"]
    if not isinstance(offset, list) or len(offset) != 2 or any(type(n) is not int for n in offset):
        raise ValueError(f"{label}: offset must be two integers.")
    if any(abs(n) > 2 ** 30 for n in offset):
        raise ValueError(f"{label}: offset exceeds Pillow's safe coordinate range (+/- 2^30).")
    for field in ("scale", "rotation"):
        number = value[field]
        if not finite_number(number):
            raise ValueError(f"{label}: {field} must be a finite number.")
    if value["scale"] <= 0:
        raise ValueError(f"{label}: scale must be positive.")
    return cast(MaskEntry, value)


def validate_transform_image(entry: MaskEntry, image: Image.Image) -> None:
    if entry["scale"] > 32_000_000 ** 0.5 / (image.width + image.height):
        raise ValueError("Transform exceeds the editor's 32-million-pixel allocation limit.")
    width, height = int(image.width * entry["scale"]), int(image.height * entry["scale"])
    if min(width, height) < 1:
        raise ValueError("Scale produces a zero-pixel image dimension.")


def validate_agents(value: object, settings: Settings, root: Path) -> Agents:
    if not isinstance(value, dict):
        raise ValueError("agents.json must map agent names to ordered skin lists.")
    names: set[str] = set()
    stems: dict[str, tuple[str, int, str]] = {}
    for name, skins in value.items():
        safe_component(name, "Agent name")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ValueError(f"Agent {name}: use letters, digits, and underscores, starting with a letter.")
        if name.casefold() in names:
            raise ValueError(f"Case-insensitive duplicate agent: {name}")
        names.add(name.casefold())
        if not isinstance(skins, list) or not skins:
            raise ValueError(f"{name}: expected at least a base skin object.")
        for index, skin in enumerate(skins):
            if not isinstance(skin, dict):
                raise ValueError(f"{name}/skin {index}: expected an object.")
            for kind, entry in skin.items():
                if kind not in MASK_SPECS:
                    raise ValueError(f"{name}/skin {index}: unsupported mask type {kind}.")
                validate_mask(entry, f"{name}/skin {index}/{kind}")
            for source in settings["SOURCE_DIRS"]:
                stem = source_stem(source, name, index).casefold()
                identity = (name, index, source)
                if stem in stems and stems[stem] != identity:
                    raise ValueError(f"Generated resource name collision: {identity} and {stems[stem]}")
                stems[stem] = identity
    # Base-art fallbacks must not alias another entry's canonical artwork.
    for source in settings["SOURCE_DIRS"]:
        for name in value:
            key = name.casefold()
            if key in stems and stems[key][:2] != (name, 0):
                raise ValueError(f"Base source filename {name}.png collides with {stems[key]}")
    selected = settings["CUR_AGENT"]
    if selected is not None and selected not in value:
        raise ValueError(f"CUR_AGENT {selected!r} is not in agents.json.")
    return cast(Agents, value)


def duplicate_hashes(agents: Agents) -> list[str]:
    hashes: dict[str, list[str]] = {}
    for name, skins in agents.items():
        for index, skin in enumerate(skins):
            for kind, entry in skin.items():
                if entry["hash"] == BLANK_HASH:
                    continue
                hashes.setdefault(entry["hash"].lower(), []).append(f"{name}/skin {index}/{kind}")
    return [f"{key}: {', '.join(entries)}" for key, entries in hashes.items() if len(entries) > 1]


def hash_from_filename(path: str | Path) -> str:
    matches = re.findall(r"(?<![A-Za-z0-9])[a-fA-F0-9]{8}(?![A-Za-z0-9])", Path(path).stem)
    if len(matches) != 1:
        raise ValueError("Filename has no single unambiguous eight-digit hash. Paste the game hash manually.")
    return matches[0].lower()


def agent_json(agents: Agents) -> bytes:
    lines = ["{"]
    for n, (name, skins) in enumerate(agents.items()):
        lines.append(f"  {json.dumps(name)}: [")
        for index, skin in enumerate(skins):
            lines.append("    {")
            for k, (kind, entry) in enumerate(skin.items()):
                ordered = {key: entry[key] for key in DEFAULT_MASK}
                lines.append(
                    f"      {json.dumps(kind)}: {json.dumps(ordered, allow_nan=False)}"
                    + ("," if k < len(skin) - 1 else "")
                )
            lines.append("    }" + ("," if index < len(skins) - 1 else ""))
        lines.append("  ]" + ("," if n < len(agents) - 1 else ""))
    lines.append("}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def decode_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        if image.width * image.height > 32_000_000:
            raise ValueError("Source artwork exceeds the editor's 32-million-pixel limit.")
        return image.convert("RGBA")


class Draft:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.settings = load_config(self.root)
        value = read_json(self.root / "agents.json")
        self.hash_warnings = replace_invalid_hashes(value)
        self._hash_repairs_pending = bool(self.hash_warnings)
        self.agents = validate_agents(value, self.settings, self.root)
        self.saved_agents = deepcopy(self.agents)
        self.saved_settings = deepcopy(self.settings)
        self.originals: dict[Path, bytes | None] = {
            self.root / name: file_bytes(self.root / name)
            for name in ("agents.json", "config.json")
        }
        self.imports: dict[Path, Image.Image] = {}

    @property
    def dirty(self) -> bool:
        return bool(
            self.imports or self._hash_repairs_pending or self.agents != self.saved_agents
            or self.settings != self.saved_settings
        )

    def validate(self) -> None:
        validate_settings(self.settings, self.root)
        validate_agents(self.agents, self.settings, self.root)
        for path in self.imports:
            if path not in self.import_targets(self.settings):
                raise ValueError(f"Resolve or discard staged import before changing its directory: {path}")
        for name, skins in self.agents.items():
            for index, skin in enumerate(skins):
                for source in self.settings["SOURCE_DIRS"]:
                    target = self.target(source, name, index)
                    path = resolve_source(self.root / source, name, index)
                    if target in self.imports:
                        image = self.imports[target]
                        for entry in skin.values():
                            validate_transform_image(entry, image)
                    elif path is not None:
                        with Image.open(path) as image:
                            for entry in skin.values():
                                validate_transform_image(entry, image)

    def import_targets(self, settings: Settings) -> set[Path]:
        return {
            self.target(source, name, index)
            for source in settings["SOURCE_DIRS"]
            for name, skins in self.agents.items()
            for index in range(len(skins))
        }

    def target(self, source: str, name: str, index: int) -> Path:
        directory = resolve_path(self.root, source)
        raw = self.root / source / (source_stem(source, name, index) + ".png")
        if raw.is_symlink():
            raise ValueError(f"Artwork destination is a symbolic link; replace it manually first: {raw}")
        target = raw.resolve()
        if target.parent != directory:
            raise ValueError(f"Artwork destination escapes source directory: {target}")
        return target

    def set_settings(self, value: object) -> None:
        settings = validate_settings(value, self.root)
        validate_agents(self.agents, settings, self.root)
        if any(path not in self.import_targets(settings) for path in self.imports):
            raise ValueError("Discard staged imports before removing or retargeting their source directories.")
        self.settings = settings

    def add_agent(self, name: str) -> None:
        candidate = deepcopy(self.agents)
        if name in candidate:
            raise ValueError(f"Agent already exists: {name}")
        candidate[name] = [{}]
        validate_agents(candidate, self.settings, self.root)
        self.agents = candidate

    def add_skin(self, name: str) -> int:
        candidate = deepcopy(self.agents)
        candidate[name].append({})
        validate_agents(candidate, self.settings, self.root)
        self.agents = candidate
        return len(candidate[name]) - 1

    def artwork(self, source: str, name: str, index: int) -> tuple[Image.Image, Path]:
        target = self.target(source, name, index)
        if target in self.imports:
            return self.imports[target], target
        path = resolve_source(self.root / source, name, index)
        if path is None:
            raise ValueError(f"No source for {name}/skin {index} in {source}. Import artwork first.")
        path = path.resolve()
        if path.parent != resolve_path(self.root, source):
            raise ValueError(f"Source symlink points outside configured directory: {path}")
        return decode_image(path), path

    def stage_image(
        self, source: str, name: str, index: int, path: Path, flip_vertical: bool = False,
    ) -> Path | None:
        target = self.target(source, name, index)
        image = decode_image(path)
        if flip_vertical:
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        resolved = resolve_source(self.root / source, name, index)
        if resolved is not None:
            existing = decode_image(resolved)
            if existing.size == image.size and existing.tobytes() == image.tobytes():
                self.imports.pop(target, None)
                return None
        self.originals.setdefault(target, file_bytes(target))
        self.imports[target] = image
        return target

    def apply_mask(self, name: str, index: int, kind: MaskKind, entry: MaskEntry, source: str) -> None:
        entry = deepcopy(entry)
        invalid_hash = "hash" in entry and not valid_hash(entry["hash"])
        if invalid_hash:
            entry["hash"] = BLANK_HASH
        validate_mask(entry)
        image, _ = self.artwork(source, name, index)
        validate_transform_image(entry, image)
        for variant in self.settings["SOURCE_DIRS"]:
            target = self.target(variant, name, index)
            path = resolve_source(self.root / variant, name, index)
            if target in self.imports:
                validate_transform_image(entry, self.imports[target])
            elif path is not None:
                with Image.open(path) as variant_image:
                    validate_transform_image(entry, variant_image)
        self.agents[name][index][kind] = entry
        if invalid_hash:
            self.hash_warnings.append(hash_warning(name, index, [kind]))

    def discard_imports(self) -> None:
        self.imports.clear()
        self.originals = {
            path: content for path, content in self.originals.items()
            if path.name in ("agents.json", "config.json") and path.parent == self.root
        }

    def save(self) -> list[Path]:
        warnings = replace_invalid_hashes(self.agents)
        self.hash_warnings.extend(warnings)
        self._hash_repairs_pending |= bool(warnings)
        self.validate()
        # Check even unchanged JSON so a stale draft cannot clobber later edits.
        for path, original in self.originals.items():
            if (path.parent == self.root or path in self.imports) and file_bytes(path) != original:
                raise ValueError(f"File changed outside the editor; reload before saving: {path}")
        writes: dict[Path, bytes] = {}
        if self._hash_repairs_pending or self.agents != self.saved_agents:
            writes[self.root / "agents.json"] = agent_json(self.agents)
        if self.settings != self.saved_settings or self.originals[self.root / "config.json"] is None:
            if self.dirty:
                writes[self.root / "config.json"] = (
                    json.dumps(self.settings, indent=2, allow_nan=False) + "\n"
                ).encode("utf-8")
        for path, image in self.imports.items():
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            writes[path] = buffer.getvalue()
        if not writes:
            return []
        temporary: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        committed: list[Path] = []
        token = uuid.uuid4().hex
        try:
            for path, content in writes.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".editor-", delete=False) as stream:
                    temporary[path] = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    backup = path.with_name(f"{path.name}.{token}.bak")
                    shutil.copy2(path, backup)
                    backups[path] = backup
            # Recheck after preparing files, before the first replacement.
            for path in writes:
                if file_bytes(path) != self.originals.get(path):
                    raise ValueError(f"File changed while preparing save: {path}")
            for path, staged in temporary.items():
                os.replace(staged, path)
                committed.append(path)
        except (OSError, ValueError) as exc:
            failures: list[str] = []
            for path in reversed(committed):
                try:
                    if path in backups:
                        shutil.copy2(backups[path], temporary[path])
                        os.replace(temporary[path], path)
                    else:
                        path.unlink()
                except OSError as restore_error:
                    failures.append(f"{path}: {restore_error}; backup: {backups.get(path, 'new file')}")
            detail = "\nRecovery needed:\n" + "\n".join(failures) if failures else "\nPrevious files restored."
            raise ValueError(f"Save failed: {exc}{detail}") from exc
        finally:
            for staged in temporary.values():
                if staged.exists():
                    staged.unlink()
        for path, content in writes.items():
            self.originals[path] = content
        self.saved_agents = deepcopy(self.agents)
        self._hash_repairs_pending = False
        self.saved_settings = deepcopy(self.settings)
        self.imports.clear()
        return list(backups.values())
