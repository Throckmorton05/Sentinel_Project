# sentinel/image_blocking/screen_monitor/hash_cache.py

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from blake3 import blake3
from PIL import Image


@dataclass
class HashEntry:
    hash: str
    timestamp: float


class WindowHashCache:
    """
    Stores the most recent image hash for each window.

    Returns True when a window should be analyzed.
    Returns False when the window has not changed.
    """

    def __init__(self) -> None:
        self._cache: dict[str, HashEntry] = {}

    def _compute_hash(
        self,
        image: Image.Image,
    ) -> str:
        """
        Compute a BLAKE3 hash of a downscaled RGB image.
        """

        resized = image.convert("RGB").resize(
            (256, 256),
            Image.Resampling.BILINEAR,
        )

        return blake3(resized.tobytes()).hexdigest()

    def should_analyze(
        self,
        window_id: str,
        image: Image.Image,
    ) -> bool:
        """
        Return True if the image has changed since the last scan.
        """

        image_hash = self._compute_hash(image)

        entry = self._cache.get(window_id)

        if entry is None:
            self._cache[window_id] = HashEntry(
                hash=image_hash,
                timestamp=monotonic(),
            )

            return True

        if entry.hash == image_hash:
            return False

        self._cache[window_id] = HashEntry(
            hash=image_hash,
            timestamp=monotonic(),
        )

        return True

    def cleanup(
        self,
        active_window_ids: set[str],
    ) -> None:
        """
        Remove cache entries for windows that no longer exist.
        """

        stale = set(self._cache) - active_window_ids

        for window_id in stale:
            del self._cache[window_id]

    def clear(self) -> None:
        """
        Clear the entire cache.
        """

        self._cache.clear()
