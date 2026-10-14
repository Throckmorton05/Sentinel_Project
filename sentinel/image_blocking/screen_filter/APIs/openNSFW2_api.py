from io import BytesIO
from time import perf_counter
import io

import numpy as np
import onnxruntime as ort
import skimage.io
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image

from sentinel.settings.paths import OPENNSFW2_ONNX_MODEL


# -------------------------------------------------------
# Settings
# -------------------------------------------------------

BLOCK_THRESHOLD = 0.85

GRID_SIZE = 3
OVERLAP = 0.25


print("Loading OpenNSFW2 ONNX model...")

session = ort.InferenceSession(
    str(OPENNSFW2_ONNX_MODEL),
    providers=["CPUExecutionProvider"],
)

input_name = session.get_inputs()[0].name
output_name = session.get_outputs()[0].name


# -------------------------------------------------------
# Tile generation
# -------------------------------------------------------


def generate_tiles(image: Image.Image) -> list[Image.Image]:
    """
    Generate overlapping tiles.

    Matches the benchmark implementation exactly.
    """

    width, height = image.size

    tile_w = width / GRID_SIZE
    tile_h = height / GRID_SIZE

    overlap_x = tile_w * OVERLAP
    overlap_y = tile_h * OVERLAP

    step_x = tile_w - overlap_x
    step_y = tile_h - overlap_y

    tiles = []

    for row in range(GRID_SIZE):
        # Ignore browser chrome / top row
        if row == 0:
            continue

        for col in range(GRID_SIZE):
            left = int(col * step_x)
            upper = int(row * step_y)

            right = int(left + tile_w)
            lower = int(upper + tile_h)

            if right > width:
                right = width
                left = max(0, right - int(tile_w))

            if lower > height:
                lower = height
                upper = max(0, lower - int(tile_h))

            tiles.append(image.crop((left, upper, right, lower)))

    return tiles


# -------------------------------------------------------
# Preprocessing + inference
# -------------------------------------------------------


def predict_tiles(tiles: list[Image.Image]) -> np.ndarray:
    """
    Batch inference using OpenNSFW2 ONNX.

    Preprocessing matches the original OpenNSFW2
    YAHOO preprocessing pipeline exactly.
    """

    batch = []

    for tile in tiles:
        if tile.mode != "RGB":
            tile = tile.convert("RGB")

        # Resize to 256x256
        tile = tile.resize((256, 256), Image.Resampling.BILINEAR)

        # JPEG round-trip
        buffer = io.BytesIO()
        tile.save(buffer, format="JPEG")
        buffer.seek(0)

        arr = skimage.io.imread(buffer).astype(np.float32)

        # Center crop
        arr = arr[16:240, 16:240, :]

        # RGB -> BGR
        arr = arr[:, :, ::-1]

        # VGG mean subtraction
        arr -= np.array([104, 117, 123], dtype=np.float32)

        batch.append(arr)

    batch = np.stack(batch, axis=0).astype(np.float32)

    outputs = session.run(
        [output_name],
        {input_name: batch},
    )[0]

    outputs = np.asarray(outputs, dtype=np.float32)

    # Column 1 = NSFW probability
    return outputs[:, 1]


# -------------------------------------------------------
# FastAPI
# -------------------------------------------------------

app = FastAPI(
    title="Sentinel OpenNSFW2 API",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": True,
    }


@app.post("/classify")
async def classify(file: UploadFile = File(...)):

    if file.content_type is None or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Uploaded file must be an image.",
        )

    try:
        image_bytes = await file.read()

        image = Image.open(BytesIO(image_bytes)).convert("RGB")

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Unable to read image.",
        )

    start = perf_counter()

    tiles = generate_tiles(image)

    tile_scores = predict_tiles(tiles)

    score = float(np.max(tile_scores))

    blocked = score >= BLOCK_THRESHOLD

    elapsed_ms = (perf_counter() - start) * 1000

    return {
        "timing_ms": round(elapsed_ms, 2),
        "score": round(score, 6),
        "blocked": blocked,
        "threshold": BLOCK_THRESHOLD,
        "tile_scores": [round(float(x), 6) for x in tile_scores],
    }
