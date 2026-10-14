from dataclasses import dataclass

from sentinel.safe_steam.installation import NATIVE_APP_ID


@dataclass
class AppEntry:
    app_id: str
    name: str
    summary: str
    categories: list[str]
    flatpak_id: str
    icon_path: str | None = None
    installer: str = "flatpak"

    @property
    def installation_id(self) -> str:
        return self.app_id if self.installer == "native_steam" else self.flatpak_id

    @classmethod
    def from_installed(cls, entry: dict) -> "AppEntry":
        if entry["id"] == NATIVE_APP_ID and entry.get("installer") == "native_steam":
            return native_steam_entry()
        return cls(entry["id"], entry["name"], "Installed application", [], entry["id"])


def native_steam_entry(icon_path: str | None = None) -> AppEntry:
    return AppEntry(
        app_id=NATIVE_APP_ID,
        name="Steam",
        summary="Play, discover, and manage your games.",
        categories=["Gaming"],
        flatpak_id="",
        icon_path=icon_path,
        installer="native_steam",
    )
