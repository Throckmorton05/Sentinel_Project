from __future__ import annotations

from io import BytesIO
from time import perf_counter

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image

from sentinel.settings.paths import HENTAI_ONNX_MODEL


# -------------------------------------------------------
# Settings
# -------------------------------------------------------

FULL_EXPLICIT_THRESHOLD = 0.90

HIGH_TILE_THRESHOLD = 0.90
VERY_HIGH_TILE_THRESHOLD = 0.98
SUPPORT_TILE_THRESHOLD = 0.70

SAFE_THRESHOLD = 0.30

GRID_SIZE = 3
OVERLAP = 0.25

# The exported ViT model uses this fixed label order.
LABELS = (
    "safe",
    "suggestive",
    "explicit",
)

# These values reproduce the original ViTImageProcessor configuration.
MODEL_IMAGE_SIZE = 224
IMAGE_MEAN = np.array([0.5, 0.5, 0.5], dtype=np.float32)
IMAGE_STD = np.array([0.5, 0.5, 0.5], dtype=np.float32)
RESCALE_FACTOR = np.float32(1.0 / 255.0)


print(f"Loading Hentai ONNX model from {HENTAI_ONNX_MODEL}...")

if not HENTAI_ONNX_MODEL.is_file():
    raise RuntimeError(
        f"Sentinel Hentai ONNX model is missing: {HENTAI_ONNX_MODEL}"
    )

session = ort.InferenceSession(
    str(HENTAI_ONNX_MODEL),
    providers=["CPUExecutionProvider"],
)

MODEL_INPUT_NAME = session.get_inputs()[0].name
MODEL_OUTPUT_NAME = session.get_outputs()[0].name

print("Hentai ONNX model loaded.")


def preprocess(image: Image.Image) -> np.ndarray:
    """Convert a PIL image to the tensor format expected by the ViT ONNX model."""

    image = image.convert("RGB")
    image = image.resize(
        (MODEL_IMAGE_SIZE, MODEL_IMAGE_SIZE),
        resample=Image.Resampling.BILINEAR,
    )

    array = np.asarray(image, dtype=np.float32)
    array *= RESCALE_FACTOR
    array = (array - IMAGE_MEAN) / IMAGE_STD

    # HWC -> CHW
    array = np.transpose(array, (2, 0, 1))

    return np.ascontiguousarray(array, dtype=np.float32)


def softmax(logits: np.ndarray) -> np.ndarray:
    """Apply a numerically stable softmax across model classes."""

    logits = logits - np.max(logits, axis=-1, keepdims=True)
    exp = np.exp(logits)
    return exp / np.sum(exp, axis=-1, keepdims=True)


def classify_batch(images: list[Image.Image]) -> list[dict[str, float]]:
    """Classify a batch with ONNX Runtime and return label -> probability maps."""

    batch = np.stack(
        [preprocess(image) for image in images],
        axis=0,
    ).astype(np.float32, copy=False)

    logits = session.run(
        [MODEL_OUTPUT_NAME],
        {MODEL_INPUT_NAME: batch},
    )[0]

    probabilities = softmax(np.asarray(logits, dtype=np.float32))

    return [
        {
            label: float(score)
            for label, score in zip(LABELS, scores)
        }
        for scores in probabilities
    ]


def split_into_tiles(image: Image.Image):
    width, height = image.size

    tile_width = width // GRID_SIZE
    tile_height = height // GRID_SIZE

    step_x = max(int(tile_width * (1 - OVERLAP)), 1)
    step_y = max(int(tile_height * (1 - OVERLAP)), 1)

    tiles = []

    row = 0
    y = 0

    while y < height:
        col = 0
        x = 0

        while x < width:
            left = x
            top = y

            right = min(left + tile_width, width)
            bottom = min(top + tile_height, height)

            left = max(0, right - tile_width)
            top = max(0, bottom - tile_height)

            tiles.append(
                {
                    "row": row,
                    "col": col,
                    "image": image.crop((left, top, right, bottom)),
                }
            )

            x += step_x
            col += 1

        y += step_y
        row += 1

    return tiles


def are_adjacent(a, b):
    return (
        abs(a["row"] - b["row"]) <= 1
        and abs(a["col"] - b["col"]) <= 1
        and a is not b
    )


app = FastAPI(
    title="Sentinel Hentai API",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": True,
        "runtime": "onnxruntime",
    }


@app.post("/classify")
async def classify(file: UploadFile = File(...)):

    if file.content_type is None or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image.")

    try:
        image = Image.open(BytesIO(await file.read())).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="Unable to read image.")

    start = perf_counter()

    tiles = split_into_tiles(image)

    batch_images = [image] + [tile["image"] for tile in tiles]

    batch_results = classify_batch(batch_images)

    full_scores = batch_results[0]
    tile_batch_results = batch_results[1:]

    tile_results = []

    for tile, scores in zip(tiles, tile_batch_results):
        tile_results.append(
            {
                "row": tile["row"],
                "col": tile["col"],
                "scores": scores,
            }
        )

    max_scores = {}

    for tile in tile_results:
        for label, score in tile["scores"].items():
            max_scores[label] = max(
                max_scores.get(label, 0.0),
                score,
            )

    full_explicit = full_scores.get("explicit", 0.0)
    full_safe = full_scores.get("safe", 0.0)

    blocked = False
    reason = None
    trigger = {}

    # Gate 1: the full image is strongly explicit.
    if full_explicit >= FULL_EXPLICIT_THRESHOLD and full_safe <= SAFE_THRESHOLD:
        blocked = True
        reason = "full_image"
        trigger = {
            "explicit": full_explicit,
            "safe": full_safe,
        }

    # Gate 2: two adjacent tiles are strongly explicit.
    if not blocked:
        high_tiles = [
            t
            for t in tile_results
            if (
                t["scores"].get("explicit", 0.0) >= HIGH_TILE_THRESHOLD
                and t["scores"].get("safe", 1.0) <= SAFE_THRESHOLD
            )
        ]

        for tile in high_tiles:
            for other in high_tiles:
                if are_adjacent(tile, other):
                    blocked = True
                    reason = "adjacent_tiles"
                    trigger = {
                        "tile1": tile,
                        "tile2": other,
                    }
                    break

            if blocked:
                break

    # Gate 3: one extremely explicit tile plus an adjacent support tile.
    if not blocked:
        for tile in tile_results:
            explicit = tile["scores"].get("explicit", 0.0)
            safe = tile["scores"].get("safe", 1.0)

            if explicit >= VERY_HIGH_TILE_THRESHOLD and safe <= SAFE_THRESHOLD:
                for other in tile_results:
                    if other is tile:
                        continue

                    if not are_adjacent(tile, other):
                        continue

                    if (
                        other["scores"].get("explicit", 0.0)
                        >= SUPPORT_TILE_THRESHOLD
                        and other["scores"].get("safe", 1.0) <= SAFE_THRESHOLD
                    ):
                        blocked = True
                        reason = "very_high_tile"
                        trigger = {
                            "main_tile": tile,
                            "support_tile": other,
                        }
                        break

            if blocked:
                break

    elapsed_ms = (perf_counter() - start) * 1000

    return {
        "timing_ms": round(elapsed_ms, 2),
        "blocked": blocked,
        "reason": reason,
        "full_image": full_scores,
        "max_tile_scores": max_scores,
        "summary": {
            "tiles": len(tile_results),
        },
        "trigger": trigger,
        "tiles": tile_results,
    }
