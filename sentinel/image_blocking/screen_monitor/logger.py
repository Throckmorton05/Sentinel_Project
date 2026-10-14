"""Send block events to sentineld without delaying protection or writing history."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, TYPE_CHECKING

from sentinel.daemon.client import SentineldError, record_screen_block

if TYPE_CHECKING:
    from sentinel.wayland.kwin_bridge import WindowInfo

_pending = deque(maxlen=256)
_condition = threading.Condition()
_thread = None


def _bounded(value, size):
    return str(value).encode('utf-8')[:size].decode('utf-8', errors='ignore')


def _deliver():
    while True:
        with _condition:
            _condition.wait_for(lambda: bool(_pending))
            event = _pending[0]
        if datetime.now(timezone.utc) - datetime.fromisoformat(event['occurred_at']) > timedelta(days=1):
            with _condition:
                if _pending and _pending[0] is event:
                    _pending.popleft()
            continue
        try:
            record_screen_block(event)
        except SentineldError:
            with _condition:
                _condition.wait(timeout=2.0)
            continue
        with _condition:
            if _pending and _pending[0] is event:
                _pending.popleft()


def log_block(*, window: WindowInfo, model: str, reason: str, scores: dict[str, Any]) -> None:
    global _thread
    try:
        safe_scores = copy.deepcopy(scores)
        if len(json.dumps(safe_scores, allow_nan=False).encode()) > 2048:
            safe_scores = {'omitted': 'Model result exceeded activity payload limit.'}
    except (TypeError, ValueError):
        safe_scores = {'omitted': 'Model result was not JSON serializable.'}
    event = {
        'event_id': uuid.uuid4().hex,
        'occurred_at': datetime.now(timezone.utc).isoformat(),
        'window_title': _bounded(window.title, 1024),
        'window_id': _bounded(window.window_id, 128),
        'app_id': _bounded(window.app_id, 256),
        'resource_name': _bounded(window.resource_name, 256),
        'pid': window.pid, 'model': _bounded(model, 128),
        'reason': _bounded(reason, 512), 'scores': safe_scores,
    }
    with _condition:
        if len(_pending) == _pending.maxlen:
            print('Sentinel activity: monitor event queue full; oldest event expired.', flush=True)
        _pending.append(event)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_deliver, name='sentinel-activity-sender', daemon=True)
            _thread.start()
        _condition.notify()
