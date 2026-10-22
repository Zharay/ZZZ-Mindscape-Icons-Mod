import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops, ImageEnhance, PngImagePlugin

MaskKind = Literal["select", "tab", "round"]


@dataclass(frozen=True)
class MaskSpec:
    filename: str
    size: tuple[int, int]
    final_size: tuple[int, int] | None = None


MASK_SPECS: dict[MaskKind, MaskSpec] = {
    "select": MaskSpec("selector.png", (512, 500)),
    "tab": MaskSpec("tab.png", (358, 128), (150, 54)),
    "round": MaskSpec("round.png", (284, 284), (150, 150)),
}


def source_stem(source_dir: str | Path, agent: str, skin_idx: int) -> str:
    skin = "" if skin_idx == 0 else "Skin" + (str(skin_idx) if skin_idx > 1 else "")
    variant = Path(source_dir).name
    return agent + skin + ("" if variant == "sources" else variant)


def source_path(source_dir: str | Path, agent: str, skin_idx: int) -> Path:
    return Path(source_dir) / (source_stem(source_dir, agent, skin_idx) + ".png")


def resolve_source(source_dir: str | Path, agent: str, skin_idx: int) -> Path | None:
    for path in (source_path(source_dir, agent, skin_idx), Path(source_dir) / f"{agent}.png"):
        if path.is_file():
            return path
    return None


def position_image(
    mask_size: tuple[int, int], image: Image.Image,
    offset: tuple[int, int], scale: float, rotation: float,
) -> Image.Image:
    image = image.convert("RGBA")
    if scale != 1.0:
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS
        )
    if rotation != 0:
        image = image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)
    x = -offset[0] + (mask_size[0] - image.width) // 2
    y = -offset[1] + (mask_size[1] - image.height) // 2
    result = Image.new("RGBA", mask_size)
    result.paste(image, (x, y))
    return result


def render_mask(
    size: tuple[int, int], mask: Image.Image, image: Image.Image,
    image_offset: tuple[int, int] = (1, 1), image_scale: float = 1.0,
    image_rotation: float = 0.0, final_size: tuple[int, int] | None = None,
    contrast: float = 1.0, saturation: float = 1.2,
) -> Image.Image:
    mask = mask.convert("RGBA")
    positioned = position_image(mask.size, image, image_offset, image_scale, image_rotation)
    # Preserve the original floor(alpha * mask_alpha / 255) and RGB semantics.
    positioned.putalpha(ImageChops.multiply(positioned.getchannel("A"), mask.getchannel("A")))
    result = Image.new("RGBA", size)
    result.alpha_composite(
        positioned, ((size[0] - mask.width) // 2, (size[1] - mask.height) // 2)
    )
    if final_size is not None:
        result = result.resize(final_size, Image.Resampling.LANCZOS)
    if contrast != 1.0 or saturation != 1.0:
        alpha = result.getchannel("A")
        rgb = result.convert("RGB")
        if contrast != 1.0:
            rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
        if saturation != 1.0:
            rgb = ImageEnhance.Color(rgb).enhance(saturation)
        result = rgb.convert("RGBA")
        result.putalpha(alpha)
    return result


def apply_mask(
    size: tuple[int, int], mask_path: str | Path, image_path: str | Path,
    output_path: str | Path, image_offset: tuple[int, int] = (1, 1),
    image_scale: float = 1.0, image_rotation: float = 0.0,
    final_size: tuple[int, int] | None = None,
) -> None:
    from config import CONTRAST_MULTIPLIER, SATURATION_MULTIPLIER

    with Image.open(mask_path) as mask, Image.open(image_path) as image:
        result = render_mask(
            size, mask, image, image_offset, image_scale, image_rotation, final_size,
            CONTRAST_MULTIPLIER, SATURATION_MULTIPLIER,
        )
    png_info = PngImagePlugin.PngInfo()
    png_info.add(b"gAMA", struct.pack(">I", 45455))
    png_info.add(b"sRGB", struct.pack("B", 0))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, pnginfo=png_info, dpi=(72, 72))
    print(f"Masked image saved as {output_path}")


def _generate(
    kind: MaskKind, source_dir: str, dest: str, agent: str, skin_idx: int,
    offset: tuple[int, int], scale: float, rotation: float,
) -> None:
    image = resolve_source(source_dir, agent, skin_idx)
    if image is None:
        print(f"Source file {Path(source_dir) / (agent + '.png')} not found! Skipping...")
        return
    spec = MASK_SPECS[kind]
    print(f"Creating {kind} for {source_stem(source_dir, agent, skin_idx)} "
          f"({offset}, {scale}, {rotation})")
    apply_mask(
        spec.size, Path("masks") / spec.filename, image,
        Path(dest) / kind / (source_stem(source_dir, agent, skin_idx) + ".png"),
        offset, scale, rotation, spec.final_size,
    )


def gen_select(
    source_dir: str, fallback_dir: str, dest: str, agent: str, skin_idx: int,
    offset: tuple[int, int] = (0, 0), scale: float = 1.0, rotation: float = 0.0,
) -> None:
    _generate("select", source_dir, dest, agent, skin_idx, offset, scale, rotation)


def gen_tab(
    source_dir: str, fallback_dir: str, dest: str, agent: str, skin_idx: int,
    offset: tuple[int, int] = (0, 0), scale: float = 1.0, rotation: float = 0.0,
) -> None:
    _generate("tab", source_dir, dest, agent, skin_idx, offset, scale, rotation)


def gen_round(
    source_dir: str, fallback_dir: str, dest: str, agent: str, skin_idx: int,
    offset: tuple[int, int] = (0, 0), scale: float = 1.0, rotation: float = 0.0,
) -> None:
    _generate("round", source_dir, dest, agent, skin_idx, offset, scale, rotation)
