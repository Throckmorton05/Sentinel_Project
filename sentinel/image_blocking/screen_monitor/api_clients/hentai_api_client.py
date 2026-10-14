# Client for communicating with the Hentai Classification API.

from io import BytesIO
from typing import Any

import requests
from PIL import Image

# Hentai API endpoint
HENTAI_API_URL = "http://127.0.0.1:8002/classify"

# Maximum time (seconds) to wait for the API
REQUEST_TIMEOUT = 10


def analyze_hentai(image: Image.Image) -> dict[str, Any]:
    """Send an image to the Hentai API and return the JSON response."""

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    response = requests.post(
        HENTAI_API_URL,
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
