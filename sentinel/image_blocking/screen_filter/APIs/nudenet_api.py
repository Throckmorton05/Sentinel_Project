from io import BytesIO
from time import perf_counter
from typing import TypedDict

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from nudenet import NudeDetector
from PIL import Image


# -------------------------------------------------------
# Settings
# -------------------------------------------------------

GRID_SIZE = 3
OVERLAP = 0.25

FEMALE_BREAST_THRESHOLD = 0.80
FEMALE_BREAST_THRESHOLD_MALE_FACE = 0.85

FEMALE_GENITAL_THRESHOLD = 0.47
MALE_GENITAL_THRESHOLD = 0.50

FACE_THRESHOLD = 0.50

MALE_GENITAL_SUPPORT_THRESHOLD = 0.20
MALE_BREAST_OVERRIDE_THRESHOLD = 0.50

print("Loading NudeNet model...")
detector = NudeDetector()
print("NudeNet model loaded.")

Detection = TypedDict(
    "Detection",
    {
        "tile": int | None,
        "row": int | None,
        "col": int | None,
        "class": str,
        "score": float,
    },
)


def split_into_tiles(image: Image.Image) -> list[tuple[int, int, int, Image.Image]]:
    width, height = image.size

    tile_width = width // GRID_SIZE
    tile_height = height // GRID_SIZE

    step_x = max(int(tile_width * (1 - OVERLAP)), 1)
    step_y = max(int(tile_height * (1 - OVERLAP)), 1)

    tiles = []

    row = 0
    y = 0
    tile_index = 0

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
                (
                    tile_index,
                    row,
                    col,
                    image.crop((left, top, right, bottom)),
                )
            )

            tile_index += 1
            x += step_x
            col += 1

        y += step_y
        row += 1

    return tiles


def detect_tiled(image: Image.Image) -> list[Detection]:
    detections: list[Detection] = []

    for tile, row, col, crop in split_into_tiles(image):
        tile_array = np.asarray(crop)

        for detection in detector.detect(tile_array):
            detections.append(
                {
                    "tile": tile,
                    "row": row,
                    "col": col,
                    "class": detection["class"],
                    "score": float(detection["score"]),
                }
            )

    return detections


def has_adjacent_detection(
    detections: list[Detection],
    label: str,
    primary_threshold: float,
    support_threshold: float,
) -> bool:

    tile_detections = [
        d for d in detections if d["tile"] is not None and d["class"] == label
    ]

    for primary in tile_detections:
        if primary["score"] < primary_threshold:
            continue

        for other in tile_detections:
            if (
                primary["row"] is None
                or primary["col"] is None
                or other["row"] is None
                or other["col"] is None
            ):
                continue

            if primary["tile"] == other["tile"]:
                continue

            if other["score"] < support_threshold:
                continue

            if (
                abs(primary["row"] - other["row"]) <= 1
                and abs(primary["col"] - other["col"]) <= 1
            ):
                return True

    return False


def filter_female_breast_detections(
    detections: list[Detection],
) -> list[Detection]:

    has_strong_male_breast = any(
        d["class"] == "MALE_BREAST_EXPOSED"
        and d["score"] >= MALE_BREAST_OVERRIDE_THRESHOLD
        for d in detections
    )

    if not has_strong_male_breast:
        return detections

    return [d for d in detections if d["class"] != "FEMALE_BREAST_EXPOSED"]


app = FastAPI(
    title="Sentinel NudeNet API",
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

    full_detections: list[Detection] = [
        {
            "tile": None,
            "row": None,
            "col": None,
            "class": d["class"],
            "score": float(d["score"]),
        }
        for d in detector.detect(np.asarray(image))
    ]

    tile_detections = detect_tiled(image)

    detections = full_detections + tile_detections
    detections = filter_female_breast_detections(detections)

    scores: dict[str, float] = {}

    for detection in detections:
        scores[detection["class"]] = max(
            scores.get(detection["class"], 0.0),
            detection["score"],
        )

    suspicious_tiles = {
        d["tile"]
        for d in detections
        if (
            d["tile"] is not None
            and d["score"] >= 0.30
            and d["class"]
            in {
                "FEMALE_BREAST_EXPOSED",
                "FEMALE_GENITALIA_EXPOSED",
                "MALE_GENITALIA_EXPOSED",
                "ANUS_EXPOSED",
            }
        )
    }

    blocked = False
    reason = None

    if scores.get("FEMALE_GENITALIA_EXPOSED", 0.0) >= FEMALE_GENITAL_THRESHOLD:
        blocked = True
        reason = "female_genitalia"

    elif has_adjacent_detection(
        detections,
        "MALE_GENITALIA_EXPOSED",
        MALE_GENITAL_THRESHOLD,
        MALE_GENITAL_SUPPORT_THRESHOLD,
    ):
        blocked = True
        reason = "male_genitalia"

    elif scores.get("ANUS_EXPOSED", 0.0) >= FEMALE_GENITAL_THRESHOLD:
        blocked = True
        reason = "anus"

    else:
        breast_threshold = FEMALE_BREAST_THRESHOLD

        if scores.get("FACE_MALE", 0.0) >= FACE_THRESHOLD:
            breast_threshold = FEMALE_BREAST_THRESHOLD_MALE_FACE

        if scores.get("FEMALE_BREAST_EXPOSED", 0.0) >= breast_threshold:
            blocked = True
            reason = "female_breast"

    elapsed_ms = (perf_counter() - start) * 1000

    return {
        "timing_ms": round(elapsed_ms, 2),
        "blocked": blocked,
        "reason": reason,
        "scores": scores,
        "summary": {
            "suspicious_tiles": len(suspicious_tiles),
            "total_detections": len(detections),
        },
        "detections": detections,
    }
