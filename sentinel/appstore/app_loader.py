import tomllib
import xml.etree.ElementTree as ET
import subprocess

from sentinel.browser_policies.restrictions import BROWSER_FLATPAK_IDS
from sentinel.safe_steam.installation import UNSUPPORTED_FLATPAK_IDS
from sentinel.appstore.models import AppEntry, native_steam_entry
from .metadata.appstream_parser import parse_appstream
from sentinel.settings.paths import APPROVED_APPS_FILE, BLOCKED_APPS_FILE

from .metadata.category_mapper import normalize_categories

CONFIG_PATH = APPROVED_APPS_FILE


def load_apps(safe_installs: bool) -> list[AppEntry]:

    try:
        metadata_apps = parse_appstream()
    except (OSError, ValueError, ET.ParseError):
        # A fresh or offline install may not have AppStream metadata yet.
        metadata_apps = []
    metadata_lookup = {}

    for metadata in metadata_apps:
        flatpak_id = metadata["flatpak_id"]

        metadata_lookup[flatpak_id] = metadata

    if safe_installs:
        apps = _load_approved_apps(metadata_lookup)
    else:
        apps = _load_flatpak_repo(metadata_lookup)

    # This fixed approved entry uses Sentinel's native installer. Keep the
    # unsupported Flatpak identity out of both catalogs, including old configs.
    icon_path = metadata_lookup.get("com.valvesoftware.Steam", {}).get("icon_path")
    return [native_steam_entry(icon_path), *apps]


def _load_approved_apps(metadata_lookup):

    with open(CONFIG_PATH, "rb") as file:
        data = tomllib.load(file)

    blocked_apps = load_blocked_apps()

    apps = []

    for app_id, app_data in data["apps"].items():
        if not app_data.get("enabled", False):
            continue

        flatpak_id = app_data["flatpak_id"]

        if flatpak_id in blocked_apps:
            continue

        metadata = metadata_lookup.get(flatpak_id)

        icon_path = None

        if metadata:
            icon_path = metadata.get("icon_path")

        app = AppEntry(
            app_id=app_id,
            name=app_data["name"],
            summary=app_data["summary"],
            categories=app_data["categories"],
            flatpak_id=flatpak_id,
            icon_path=icon_path,
        )

        apps.append(app)

    return apps


def load_blocked_apps():

    with open(BLOCKED_APPS_FILE, "rb") as file:
        data = tomllib.load(file)

    apps = data.get("apps", {})

    blocked_ids = set(BROWSER_FLATPAK_IDS | UNSUPPORTED_FLATPAK_IDS)

    for app_data in apps.values():
        flatpak_id = app_data.get("flatpak_id")

        if flatpak_id:
            blocked_ids.add(flatpak_id)

    return blocked_ids


def _load_flatpak_repo(metadata_lookup) -> list[AppEntry]:

    result = subprocess.run(
        [
            "flatpak",
            "remote-ls",
            "--system",
            "--app",
            "--columns=name,application,description",
            "flathub",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    apps = []

    for line in result.stdout.splitlines():
        parts = line.split("\t")

        if len(parts) < 3:
            continue

        name, flatpak_id, summary = parts[:3]
        if flatpak_id in BROWSER_FLATPAK_IDS | UNSUPPORTED_FLATPAK_IDS:
            continue

        metadata = metadata_lookup.get(flatpak_id)

        icon_path = None
        categories = []

        if metadata:
            icon_path = metadata.get("icon_path")

            categories = metadata.get(
                "categories",
                [],
            )

            categories = normalize_categories(categories)

        app = AppEntry(
            app_id=flatpak_id,
            name=name,
            summary=summary,
            categories=categories,
            flatpak_id=flatpak_id,
            icon_path=icon_path,
        )

        apps.append(app)

    return apps
