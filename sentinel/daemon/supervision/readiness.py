"""Check the installed inference services before the initial monitor launch."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

INFERENCE_HEALTH_URLS = tuple(f"http://127.0.0.1:{port}/health" for port in range(8000, 8004))

# These local probes must not be sent through a system HTTP proxy.
_health_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def inference_api_health() -> dict[int, dict[str, bool]]:
    """Bounded local probes distinguish loading models from leftover servers."""
    results = {}
    for port, url in zip(range(8000, 8004), INFERENCE_HEALTH_URLS):
        result = {'reachable': False, 'ready': False}
        try:
            with _health_opener.open(url, timeout=0.5) as response:
                result['reachable'] = True
                data = json.loads(response.read(4096))
                result['ready'] = (response.status == 200 and isinstance(data, dict)
                                   and data.get('status') == 'ok' and data.get('model_loaded') is True)
        except urllib.error.HTTPError:
            result['reachable'] = True
        except (OSError, urllib.error.URLError, ValueError):
            pass
        results[port] = result
    return results


def _inference_apis_ready() -> bool:
    for url in INFERENCE_HEALTH_URLS:
        try:
            with _health_opener.open(url, timeout=1.0) as response:
                data = json.loads(response.read(4096))
                if response.status != 200 or not isinstance(data, dict):
                    return False
                if data.get("status") != "ok" or data.get("model_loaded") is not True:
                    return False
        except (OSError, urllib.error.URLError, ValueError):
            return False
    return True
