from io import BytesIO
import onnxruntime as ort
import numpy as np
from time import perf_counter
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image

from sentinel.settings.paths import ANIME_REAL_MODEL

# DRAW_API = "http://127.0.0.1:8000/classify"


GRID_SIZE = 3
OVERLAP = 0.25

DRAWN_THRESHOLD = 0.90

PHOTO_VETO_THRESHOLD = 0.70

MAX_STRONG_PHOTO_TILES = 1


print("Loading draw model...")

session = ort.InferenceSession(
    str(ANIME_REAL_MODEL),
    providers=["CPUExecutionProvider"],
)

print("Draw model loaded.")


def preprocess(image: Image.Image) -> np.ndarray:
    image = image.resize((384, 384))
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = array.transpose((2, 0, 1))
    return array


def split_into_tiles(
    image: Image.Image,
) -> list[tuple[int, int, Image.Image]]:
    width, height = image.size

    tile_w = width / GRID_SIZE
    tile_h = height / GRID_SIZE

    overlap_x = tile_w * OVERLAP
    overlap_y = tile_h * OVERLAP

    tiles = []

    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            left = int(max(0, col * tile_w - overlap_x))
            upper = int(max(0, row * tile_h - overlap_y))

            right = int(min(width, (col + 1) * tile_w + overlap_x))
            lower = int(min(height, (row + 1) * tile_h + overlap_y))

            tiles.append(
                (
                    row,
                    col,
                    image.crop((left, upper, right, lower)),
                )
            )

    return tiles


def classify_batch(
    images: list[Image.Image],
) -> np.ndarray:
    tensors = [preprocess(image) for image in images]

    batch = np.stack(tensors, axis=0).astype(np.float32)

    outputs = session.run(
        None,
        {session.get_inputs()[0].name: batch},
    )

    return np.asarray(outputs[0], dtype=np.float32)


app = FastAPI(
    title="Sentinel Draw Detection API",
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
    """
    Classify an image as either a photograph or a drawing.
    """

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

    # Generate the 3x3 tiles.
    tiles = split_into_tiles(image)

    # Full image + all 9 tiles = batch of 10 images.
    batch_images = [image] + [tile for _, _, tile in tiles]

    # Run the entire batch through ONNX in one inference call.
    batch_scores = classify_batch(batch_images)

    # First result belongs to the full image.
    full_drawn = float(batch_scores[0][0])
    full_photo = float(batch_scores[0][1])

    # Remaining results belong to the tiles.
    tile_scores = batch_scores[1:]

    tile_results = []

    for (row, col, _), scores in zip(tiles, tile_scores):
        tile_results.append(
            {
                "row": row,
                "col": col,
                "drawn": float(scores[0]),
                "photo": float(scores[1]),
            }
        )

    usable_tiles = [t for t in tile_results if t["row"] != 0]

    drawn_tiles = sum(t["drawn"] >= DRAWN_THRESHOLD for t in usable_tiles)

    photo_tiles = len(usable_tiles) - drawn_tiles

    strong_photo_tiles = sum(t["photo"] >= PHOTO_VETO_THRESHOLD for t in usable_tiles)

    average_drawn = sum(t["drawn"] for t in tile_results) / len(tile_results)

    max_drawn = max(t["drawn"] for t in tile_results)

    is_drawing = (
        drawn_tiles > photo_tiles and strong_photo_tiles <= MAX_STRONG_PHOTO_TILES
    )

    elapsed_ms = (perf_counter() - start) * 1000

    return {
        "timing_ms": round(elapsed_ms, 2),
        "prediction": ("drawing" if is_drawing else "photo"),
        "full_image": {
            "drawn": full_drawn,
            "photo": full_photo,
        },
        "summary": {
            "average_drawn": average_drawn,
            "max_drawn": max_drawn,
            "drawn_tiles": drawn_tiles,
            "photo_tiles": photo_tiles,
            "strong_photo_tiles": strong_photo_tiles,
        },
        "tiles": tile_results,
    }
