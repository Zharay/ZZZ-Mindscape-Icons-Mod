"""Optional local Real-ESRGAN inference for enlarged artwork."""

from __future__ import annotations

import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from pickle import UnpicklingError
from typing import TYPE_CHECKING, Literal
from urllib.request import urlopen

from PIL import Image

if TYPE_CHECKING:
    from spandrel import ImageModelDescriptor

Upscaler = Literal["pillow", "realesrgan"]
MODEL_NAME = "RealESRGAN_x4plus_anime_6B.pth"
MODEL_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/" + MODEL_NAME
)
MODEL_PATH = Path(__file__).resolve().parent / "models" / MODEL_NAME
TILE_SIZE = 128
TILE_PAD = 16


class UpscaleError(RuntimeError):
    """An explicitly selected ML backend could not run."""


def _download_model(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {MODEL_NAME} (first use only)...", flush=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            with urlopen(MODEL_URL, timeout=60) as response:
                shutil.copyfileobj(response, output)
        temporary.replace(path)
    except OSError as exc:
        raise UpscaleError(f"Unable to download {MODEL_NAME}: {exc}. Retry with internet access.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def load_model() -> ImageModelDescriptor:
    try:
        import torch
        from spandrel import ImageModelDescriptor, ModelLoader
    except ImportError as exc:
        raise UpscaleError(
            "Real-ESRGAN needs optional dependencies. Run "
            "'python -m pip install -r requirements-upscaler.txt', then restart."
        ) from exc
    if not torch.cuda.is_available():
        raise UpscaleError(
            "Real-ESRGAN requires an NVIDIA GPU, a current driver and CUDA-enabled PyTorch. "
            "Install requirements-upscaler.txt or select the pillow upscaler."
        )
    if not MODEL_PATH.is_file():
        _download_model(MODEL_PATH)
    try:
        # Load only tensor weights, never executable objects from the checkpoint.
        checkpoint = torch.load(MODEL_PATH, map_location="cpu", weights_only=True)
        weights = checkpoint["params_ema"] if "params_ema" in checkpoint else checkpoint["params"]
        model = ModelLoader().load_from_state_dict(weights)
        if not isinstance(model, ImageModelDescriptor) or (
            model.scale != 4 or model.input_channels != 3 or model.output_channels != 3
        ):
            raise UpscaleError(f"{MODEL_PATH} must contain the RGB 4x anime model.")
        return model.cuda().eval()
    except (OSError, ValueError, KeyError, RuntimeError, UnpicklingError, EOFError) as exc:
        raise UpscaleError(f"Unable to load Real-ESRGAN model {MODEL_PATH}: {exc}") from exc


@lru_cache(maxsize=2)
def _upscale_rgb(size: tuple[int, int], pixels: bytes) -> Image.Image:
    model = load_model()
    import numpy as np
    import torch

    image = Image.frombytes("RGB", size, pixels)
    result = Image.new("RGB", (image.width * 4, image.height * 4))
    try:
        with torch.inference_mode():
            for top in range(0, image.height, TILE_SIZE):
                for left in range(0, image.width, TILE_SIZE):
                    right = min(left + TILE_SIZE, image.width)
                    bottom = min(top + TILE_SIZE, image.height)
                    x0, y0 = max(left - TILE_PAD, 0), max(top - TILE_PAD, 0)
                    x1 = min(right + TILE_PAD, image.width)
                    y1 = min(bottom + TILE_PAD, image.height)
                    tile = np.array(image.crop((x0, y0, x1, y1)), dtype=np.float32) / 255.0
                    tensor = torch.from_numpy(tile).permute(2, 0, 1).unsqueeze(0).cuda()
                    output = model(tensor)[0, :, (top - y0) * 4:(bottom - y0) * 4,
                                           (left - x0) * 4:(right - x0) * 4]
                    array = output.clamp(0, 1).mul(255).round().byte().permute(1, 2, 0).cpu().numpy()
                    result.paste(Image.fromarray(array), (left * 4, top * 4))
    except RuntimeError as exc:
        raise UpscaleError(
            f"Real-ESRGAN GPU inference failed: {exc}. "
            "Free GPU memory/check your NVIDIA driver, or select pillow."
        ) from exc
    return result


def resize_artwork(
    image: Image.Image, size: tuple[int, int], backend: Upscaler = "pillow",
) -> Image.Image:
    if backend not in ("pillow", "realesrgan"):
        raise ValueError(f"Unknown image upscaler: {backend}")
    if min(size) < 1:
        raise ValueError("Resize dimensions must be positive.")
    if backend == "pillow" or (size[0] <= image.width and size[1] <= image.height):
        return image.resize(size, Image.Resampling.LANCZOS)
    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    while rgb.width < size[0] or rgb.height < size[1]:
        rgb = _upscale_rgb(rgb.size, rgb.tobytes())
    result = rgb.resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    # Alpha is coverage, not artwork; ML would invent unwanted opaque edges.
    result.putalpha(rgba.getchannel("A").resize(size, Image.Resampling.LANCZOS))
    return result
