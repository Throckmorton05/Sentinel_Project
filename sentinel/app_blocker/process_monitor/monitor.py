from __future__ import annotations

import psutil
from sentinel.guardian_apps import desktop

from sentinel.app_blocker.process_monitor.policy import ProcessBlockPolicy
from sentinel.vm_protection import vm_process


# PID -> (process creation time, inherited Flatpak ID). This makes Flatpak
# matching cheap after the first scan instead of rereading /proc/*/environ four
# times per second for every long-lived process.
_flatpak_cache: dict[int, tuple[float | None, str | None]] = {}


def kill_process(proc: psutil.Process) -> bool:
    try:
        print(
            f"sentineld app blocker: killing blocked process "
            f"pid={proc.pid} exe={proc.info.get('exe')}",
            flush=True,
        )
        proc.kill()
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _flatpak_id(proc: psutil.Process) -> str | None:
    create_time = proc.info.get("create_time")
    cached = _flatpak_cache.get(proc.pid)
    if cached is not None and cached[0] == create_time:
        return cached[1]

    try:
        flatpak_id = proc.environ().get("FLATPAK_ID")
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        flatpak_id = None

    _flatpak_cache[proc.pid] = (create_time, flatpak_id)
    return flatpak_id


def scan_processes(policy: ProcessBlockPolicy, *, uid: int | None = None) -> int:
    """Kill processes matching the supplied daemon-owned block policy."""
    if not (policy.native_executables or policy.flatpak_ids or policy.snap_instances
            or policy.steam_launchers or policy.vm_tools):
        _flatpak_cache.clear()
        return 0

    killed = 0
    seen_pids: set[int] = set()

    steam_roots = desktop.steam_roots(uid) if policy.steam_launchers and uid is not None else []
    for proc in psutil.process_iter(["pid", "exe", "create_time", "uids"]):
        seen_pids.add(proc.pid)
        exe = proc.info.get("exe")
        blocked = bool(exe and exe in policy.native_executables)

        if not blocked and policy.vm_tools:
            try:
                blocked = vm_process(proc)
            except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        if not blocked and policy.flatpak_ids:
            flatpak_id = _flatpak_id(proc)
            blocked = bool(flatpak_id and flatpak_id in policy.flatpak_ids)

        # Package families are scoped to the configured desktop user. Never
        # attach a rule to the shared snap runner or indiscriminately kill shells.
        try:
            package_user = uid is not None and proc.info.get('uids') is not None and proc.info['uids'].real == uid
            if not blocked and package_user and policy.snap_instances:
                blocked = desktop.snap_process(proc) in policy.snap_instances
            if not blocked and package_user and policy.steam_launchers:
                script = desktop.interpreter_script(proc)
                blocked = bool(script in policy.steam_launchers
                    or desktop.is_steam_path(script, uid, roots=steam_roots)
                    or desktop.steam_runtime(exe, uid, roots=steam_roots))
        except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        if blocked and kill_process(proc):
            killed += 1

    for pid in set(_flatpak_cache) - seen_pids:
        _flatpak_cache.pop(pid, None)

    return killed


def monitor(policy_provider=None, *, interval: float = 0.25) -> None:
    """Compatibility entry point.

    Production enforcement is owned by sentineld. A caller must provide a
    policy provider explicitly instead of this process reading protected policy
    files on its own.
    """
    if policy_provider is None:
        raise RuntimeError(
            "The Sentinel process monitor is managed by sentineld; "
            "start sentineld instead of running sentinel-process-monitor directly."
        )

    import time

    while True:
        scan_processes(policy_provider())
        time.sleep(interval)
