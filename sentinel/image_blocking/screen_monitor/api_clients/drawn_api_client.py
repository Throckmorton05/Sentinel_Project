# Client for communicating with the Draw Classification API.

from io import BytesIO
from typing import Any, cast
import requests
from PIL import Image

# Draw API endpoint
DRAW_API_URL = "http://127.0.0.1:8001/classify"

# Maximum time (seconds) to wait for the API
REQUEST_TIMEOUT = 10


def analyze_draw(image: Image.Image) -> dict[str, Any]:

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    response = requests.post(
        DRAW_API_URL,
        files={
            "file": (
                "capture.png",
                buffer,
                "image/png",
            )
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    return response.json()
