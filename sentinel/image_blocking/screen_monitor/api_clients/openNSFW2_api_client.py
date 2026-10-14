# Client for communicating with the OpenNSFW2 API.

from io import BytesIO
from typing import Any

import requests
from PIL import Image

# OpenNSFW2 API endpoint
OPENNSFW2_API_URL = "http://127.0.0.1:8000/classify"

# Maximum time (seconds) to wait for the API
REQUEST_TIMEOUT = 10


def analyze_opennsfw2(image: Image.Image) -> dict[str, Any]:
    """
    Send a PIL Image to the OpenNSFW2 API.

    Returns the API JSON response.
    """

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    response = requests.post(
        OPENNSFW2_API_URL,
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
