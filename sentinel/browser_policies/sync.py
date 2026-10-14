"""Compatibility entry point: root-owned policy writes belong to sentineld."""
import json
from sentinel.daemon.client import request


def sync_policies() -> None:
    print(json.dumps(request("browser.sync"), indent=2))


if __name__ == "__main__":
    sync_policies()
