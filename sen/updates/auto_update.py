import calendar
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict


from sentinel.settings.paths import (
    CONFIG_DIR,
    SYSTEMD_SYSTEM_DIR,
)

CONFIG_PATH = CONFIG_DIR / "auto-update.json"


SERVICE_NAME = "sentinel-auto-update.service"
TIMER_NAME = "sentinel-auto-update.timer"

SERVICE_PATH = SYSTEMD_SYSTEM_DIR / SERVICE_NAME
TIMER_PATH = SYSTEMD_SYSTEM_DIR / TIMER_NAME

REBOOT_REQUIRED_PATH = Path("/run/reboot-required")

DEFAULT_UPDATE_TIME = "03:00"

VALID_CADENCES = {
    "daily",
    "weekly",
    "biweekly",
    "monthly",
}


class AutoUpdateConfig(TypedDict):
    enabled: bool
    cadence: str
    time: str
    auto_restart: bool
    last_success: str | None


class AutoUpdateStatus(TypedDict):
    enabled: bool
    cadence: str
    time: str
    last_success: str | None


def _default_config() -> AutoUpdateConfig:
    return {
        "enabled": False,
        "cadence": "weekly",
        "time": DEFAULT_UPDATE_TIME,
        "auto_restart": False,
        "last_success": None,
    }


def _load_config() -> AutoUpdateConfig:
    config = _default_config()

    if not CONFIG_PATH.exists():
        return config

    try:
        saved_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config

    if not isinstance(saved_config, dict):
        return config

    enabled = saved_config.get("enabled")
    cadence = saved_config.get("cadence")
    update_time = saved_config.get("time")
    auto_restart = saved_config.get("auto_restart")
    last_success = saved_config.get("last_success")

    if isinstance(enabled, bool):
        config["enabled"] = enabled

    if isinstance(cadence, str) and cadence in VALID_CADENCES:
        config["cadence"] = cadence

    if isinstance(update_time, str):
        try:
            config["time"] = _validate_time(update_time)
        except ValueError:
            pass

    if isinstance(auto_restart, bool):
        config["auto_restart"] = auto_restart

    if isinstance(last_success, str) or last_success is None:
        config["last_success"] = last_success

    return config


def _save_config(config: AutoUpdateConfig) -> None:
    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o755,
    )

    CONFIG_PATH.write_text(
        json.dumps(config, indent=4) + "\n",
        encoding="utf-8",
    )

    CONFIG_PATH.chmod(0o644)


def _validate_time(update_time: str) -> str:
    try:
        parsed = datetime.strptime(
            update_time,
            "%H:%M",
        )
    except ValueError as exc:
        raise ValueError("Time must use 24-hour HH:MM format.") from exc

    return parsed.strftime("%H:%M")


def _validate_cadence(cadence: str) -> str:
    cadence = cadence.lower()

    if cadence not in VALID_CADENCES:
        raise ValueError("Cadence must be daily, weekly, biweekly, or monthly.")

    return cadence


def _add_one_month(value: datetime) -> datetime:
    year = value.year
    month = value.month + 1

    if month == 13:
        month = 1
        year += 1

    last_day = calendar.monthrange(year, month)[1]

    day = min(
        value.day,
        last_day,
    )

    return value.replace(
        year=year,
        month=month,
        day=day,
    )


def _update_is_due(config: AutoUpdateConfig) -> bool:
    last_success = config.get("last_success")

    if not last_success:
        return True

    try:
        previous = datetime.fromisoformat(last_success)
    except ValueError:
        return True

    if previous.tzinfo is None:
        previous = previous.astimezone()

    now = datetime.now().astimezone()

    cadence = config["cadence"]

    if cadence == "daily":
        next_update = previous + timedelta(days=1)

    elif cadence == "weekly":
        next_update = previous + timedelta(days=7)

    elif cadence == "biweekly":
        next_update = previous + timedelta(days=14)

    elif cadence == "monthly":
        next_update = _add_one_month(previous)

    else:
        return False

    return now >= next_update


def _service_contents() -> str:
    python_path = Path(sys.executable).resolve()
    script_path = Path(__file__).resolve()

    return f"""[Unit]
Description=Sentinel Automatic System Update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={python_path} {script_path} --run
"""


def _timer_contents(update_time: str) -> str:
    hour, minute = update_time.split(":")

    return f"""[Unit]
Description=Sentinel Automatic Update Timer

[Timer]
OnCalendar=*-*-* {hour}:{minute}:00
Persistent=true
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""


def _install_systemd_units(update_time: str) -> None:
    SERVICE_PATH.write_text(
        _service_contents(),
        encoding="utf-8",
    )

    TIMER_PATH.write_text(
        _timer_contents(update_time),
        encoding="utf-8",
    )

    SERVICE_PATH.chmod(0o644)
    TIMER_PATH.chmod(0o644)

    subprocess.run(
        ["systemctl", "daemon-reload"],
        check=True,
    )


def enable_auto_updates(
    cadence: str,
    update_time: str = DEFAULT_UPDATE_TIME,
) -> None:
    cadence = _validate_cadence(cadence)
    update_time = _validate_time(update_time)

    config = _load_config()

    _install_systemd_units(update_time)

    subprocess.run(
        [
            "systemctl",
            "enable",
            "--now",
            TIMER_NAME,
        ],
        check=True,
    )

    config["enabled"] = True
    config["cadence"] = cadence
    config["time"] = update_time

    _save_config(config)


def disable_auto_updates() -> None:
    config = _load_config()

    config["enabled"] = False

    _save_config(config)

    subprocess.run(
        [
            "systemctl",
            "disable",
            "--now",
            TIMER_NAME,
        ],
        check=False,
    )


def enable_auto_restart() -> None:
    config = _load_config()

    config["auto_restart"] = True

    _save_config(config)


def disable_auto_restart() -> None:
    config = _load_config()

    config["auto_restart"] = False

    _save_config(config)


def run_scheduled_update() -> None:
    config = _load_config()

    if not config.get("enabled", False):
        print("[Sentinel] Automatic updates are disabled.")
        return

    if not _update_is_due(config):
        print("[Sentinel] Automatic update is not due yet.")
        return

    print("[Sentinel] Starting automatic system update.")

    # The same bounded, unattended update implementation as sen update-sys.
    # Do not record success or reboot after a failed index refresh/upgrade.
    from sentinel.daemon.operations.system_ops import _system_update
    _system_update()

    config["last_success"] = datetime.now().astimezone().isoformat()

    _save_config(config)

    print("[Sentinel] Automatic update completed successfully.")

    if config.get("auto_restart", False) and REBOOT_REQUIRED_PATH.exists():
        print("[Sentinel] Restart required. Automatic restart enabled.")

        subprocess.run(
            ["systemctl", "reboot"],
            check=True,
        )


def get_auto_update_status() -> AutoUpdateStatus:
    config = _load_config()

    return {
        "enabled": bool(config.get("enabled", False)),
        "cadence": config.get("cadence", "weekly"),
        "time": config.get("time", DEFAULT_UPDATE_TIME),
        "last_success": config.get("last_success"),
    }


def get_auto_restart_status() -> bool:
    config = _load_config()

    return bool(config.get("auto_restart", False))


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--run":
        run_scheduled_update()
        return

    print("This module is managed through the sen command.")


if __name__ == "__main__":
    main()
