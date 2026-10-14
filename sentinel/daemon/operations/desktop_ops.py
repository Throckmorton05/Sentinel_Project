"""Explicit desktop configuration operations behind the daemon policy gate."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from sen.updates import auto_update
from sentinel.daemon.core.constants import RUNUSER
from sentinel.daemon.core.session import _peer_user
from sentinel.settings.paths import SDDM_AUTOLOGIN_CONFIG


def _required_text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"Missing or invalid {key}.")
    return value


def configure_desktop(action: str, params: dict[str, Any], uid: int) -> dict:
    """Called only after request_handler has authorized the mutation."""
    if action == "updates.enable":
        auto_update.enable_auto_updates(
            _required_text(params, "cadence"), _required_text(params, "time")
        )
    elif action == "updates.disable":
        auto_update.disable_auto_updates()
    elif action == "updates.restart_enable":
        auto_update.enable_auto_restart()
    elif action == "updates.restart_disable":
        auto_update.disable_auto_restart()
    elif action == "autologin.enable":
        import pam

        user = _peer_user(uid)
        password = _required_text(params, "password")
        if not pam.pam().authenticate(user.pw_name, password, service="login"):
            raise ValueError("Desktop user authentication failed.")
        SDDM_AUTOLOGIN_CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        SDDM_AUTOLOGIN_CONFIG.write_text(
            f"[Autologin]\nUser={user.pw_name}\nSession=plasma.desktop\nRelogin=false\n",
            encoding="utf-8",
        )
        SDDM_AUTOLOGIN_CONFIG.chmod(0o644)
    elif action == "autologin.disable":
        SDDM_AUTOLOGIN_CONFIG.unlink(missing_ok=True)
    elif action in {"autostart.add", "autostart.remove"}:
        user = _peer_user(uid)
        # Never copy, write, unlink or chown user-controlled paths as root.
        # The helper runs as the socket peer, from the installed protected module.
        subprocess.run(
            [
                RUNUSER,
                "-u",
                user.pw_name,
                "--",
                sys.executable,
                "-I",
                "-m",
                "sen.autostart",
                action.removeprefix("autostart."),
                _required_text(params, "name"),
            ],
            check=True,
        )
    else:
        raise ValueError("Unknown desktop configuration action.")
    return {"status": "complete", "action": action}
