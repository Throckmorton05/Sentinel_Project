"""Update discovery and mutations are handled by sentineld."""
from sentinel.daemon import client


class UpdateCheckError(RuntimeError):
    """Raised when Sentinel cannot check for available updates."""


def get_all_updates():
    try:
        return client.get_appstore_updates()["updates"]
    except client.SentineldError as exc:
        raise UpdateCheckError(str(exc)) from exc


def update_flatpak_app(ref, *, guardian_pin=None):
    return client.update_flatpak(ref, guardian_pin=guardian_pin)


def update_system_package(package_name, *, guardian_pin=None):
    return client.update_system_package(package_name, guardian_pin=guardian_pin)


def update_all(*, guardian_pin=None):
    return client.update_appstore_all(guardian_pin=guardian_pin)
