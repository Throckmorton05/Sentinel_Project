# sentinel/image_blocking/screen_monitor/inference.py

from __future__ import annotations

import time

import requests

from typing import Any

from sentinel.image_blocking.screen_monitor.api_clients.openNSFW2_api_client import (
    analyze_opennsfw2,
)

from sentinel.image_blocking.screen_monitor.api_clients.drawn_api_client import (
    analyze_draw,
)

from sentinel.image_blocking.screen_monitor.api_clients.nudenet_api_client import (
    analyze_nudenet,
)

from sentinel.image_blocking.screen_monitor.api_clients.hentai_api_client import (
    analyze_hentai,
)


def analyze(image: Any) -> dict[str, Any]:
    """
    Run the complete Sentinel inference pipeline on an image.

    Returns the same final NSFW result produced by monitor.py.
    """

    pipeline_start = time.perf_counter()

    try:
        # -----------------------------------------------------
        # Stage 1: OpenNSFW2 gate
        # -----------------------------------------------------

        start = time.perf_counter()

        opennsfw_result = analyze_opennsfw2(image)

        opennsfw_ms = (time.perf_counter() - start) * 1000

        gate_score = opennsfw_result["score"]
        gate_blocked = opennsfw_result["blocked"]

        print(f"  OpenNSFW2: {gate_score:.3f} ({opennsfw_ms:.2f} ms)")

        # Safe image — don't run any more models.
        if not gate_blocked:
            pipeline_time = (time.perf_counter() - pipeline_start) * 1000

            print(" ✅ Allowed (Stage 1)")
            print(f"  Total: {pipeline_time:.2f} ms")

            return {
                "blocked": False,
                "model": None,
                "reason": None,
                "result": opennsfw_result,
            }

        print(
            f"  Stage 1 Triggered "
            f"({gate_score:.3f} >= "
            f"{opennsfw_result['threshold']:.2f})"
        )

        # -----------------------------------------------------
        # Stage 2: Draw classifier
        # -----------------------------------------------------

        start = time.perf_counter()

        draw_result = analyze_draw(image)

        draw_time = (time.perf_counter() - start) * 1000

        prediction = draw_result["prediction"]
        score = draw_result["summary"]["average_drawn"]

        print(f"  Draw: {prediction} ({score:.3f}) ({draw_time:.2f} ms)")

        # -----------------------------------------------------
        # Stage 3: Hentai or NudeNet
        # -----------------------------------------------------

        start = time.perf_counter()

        if prediction == "drawing":
            nsfw_result = analyze_hentai(image)
            model = "Hentai"

            model_time = (time.perf_counter() - start) * 1000

            print(f"  Model: Hentai ({model_time:.2f} ms)")

        else:
            nsfw_result = analyze_nudenet(image)
            model = "NudeNet"

            model_time = (time.perf_counter() - start) * 1000

            print(f"  Model: NudeNet ({model_time:.2f} ms)")

        # -----------------------------------------------------
        # Final result
        # -----------------------------------------------------

        pipeline_time = (time.perf_counter() - pipeline_start) * 1000

        if nsfw_result["blocked"]:
            print(f" ❌ BLOCKED ({nsfw_result['reason']})")

        else:
            print(" ✅ Allowed")

        print(f"  Total: {pipeline_time:.2f} ms")

        return {
            "blocked": nsfw_result["blocked"],
            "model": model,
            "reason": nsfw_result.get("reason"),
            "result": nsfw_result,
            "draw_prediction": prediction,
            "draw_score": score,
            "opennsfw": opennsfw_result,
        }

    except requests.RequestException as e:
        print(f"API request failed: {e}")
        raise
