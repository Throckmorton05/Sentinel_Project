from typing import TypedDict


class PackageDefinition(TypedDict):
    type: str
    packages: list[str]


APPROVED_PACKAGES: dict[str, PackageDefinition] = {
    # The native Chrome package must already be available from the configured
    # trusted APT repository. Flatpak Chrome is intentionally unsupported.
    "chrome": {"type": "apt", "packages": ["google-chrome-stable"]},
    # Supported native Steam; approved in Self Lock as well as Unlocked.
    # The ISO must configure Ubuntu multiverse and Steam's i386 dependencies.
    "steam": {"type": "apt", "packages": ["steam-installer"]},
    ## Essential Coding tools:
    "git": {
        "type": "apt",
        "packages": ["git"],
    },
    "curl": {
        "type": "apt",
        "packages": ["curl"],
    },
    "build-essential": {
        "type": "apt",
        "packages": ["build-essential"],
    },
    "cmake": {
        "type": "apt",
        "packages": ["cmake"],
    },
    "ninja": {
        "type": "apt",
        "packages": ["ninja-build"],
    },
    "python": {
        "type": "apt",
        "packages": [
            "python3",
            "python3-pip",
            "python3-venv",
        ],
    },
    "vscode": {
        "type": "flatpak",
        "packages": ["com.visualstudio.code"],
    },
    "neovim": {
        "type": "custom",
        "packages": ["neovim"],
    },
    "unity": {
        "type": "custom",
        "packages": ["unity"],
    },
    "qt6": {
        "type": "apt",
        "packages": [
            "qt6-base-dev",
            "qt6-tools-dev",
            "qt6-tools-dev-tools",
            "qt6-wayland",
            "designer-qt6",
        ],
    },
    "pyqt6": {
        "type": "apt",
        "packages": [
            "python3-pyqt6",
        ],
    },
    "pipx": {
        "type": "apt",
        "packages": ["pipx"],
    },
    "poetry": {
        "type": "pipx",
        "packages": ["poetry"],
    },
    "qt-designer": {
        "type": "apt",
        "packages": [
            "qt6-tools-dev-tools",
        ],
    },
    "qtcreator": {
        "type": "apt",
        "packages": ["qtcreator"],
    },
    "ripgrep": {
        "type": "apt",
        "packages": ["ripgrep"],
    },
    "fd": {
        "type": "apt",
        "packages": ["fd-find"],
    },
    "unzip": {
        "type": "apt",
        "packages": ["unzip"],
    },
    "zip": {
        "type": "apt",
        "packages": ["zip"],
    },
    "wl-clipboard": {
        "type": "apt",
        "packages": ["wl-clipboard"],
    },
    "xclip": {
        "type": "apt",
        "packages": ["xclip"],
    },
    "clang": {
        "type": "apt",
        "packages": ["clang"],
    },
    "gdb": {
        "type": "apt",
        "packages": ["gdb"],
    },
    "valgrind": {
        "type": "apt",
        "packages": ["valgrind"],
    },
    "openssh": {
        "type": "apt",
        "packages": ["openssh-client"],
    },
    "tmux": {
        "type": "apt",
        "packages": ["tmux"],
    },
    "zsh": {
        "type": "apt",
        "packages": ["zsh"],
    },
    "fish": {
        "type": "apt",
        "packages": ["fish"],
    },
    "wget": {
        "type": "apt",
        "packages": ["wget"],
    },
    "htop": {
        "type": "apt",
        "packages": ["htop"],
    },
    "tree": {
        "type": "apt",
        "packages": ["tree"],
    },
    "jq": {
        "type": "apt",
        "packages": ["jq"],
    },
    "ffmpeg": {
        "type": "apt",
        "packages": ["ffmpeg"],
    },
    "sqlite": {
        "type": "apt",
        "packages": ["sqlite3"],
    },
    "openjdk": {
        "type": "apt",
        "packages": ["default-jdk"],
    },
    "rsync": {
        "type": "apt",
        "packages": ["rsync"],
    },
    "syncthing": {
        "type": "apt",
        "packages": ["syncthing"],
    },
    "reaper": {
        "type": "custom",
        "packages": ["reaper"],
    },
    # Native desktop launcher (Rofi 2.x supports Wayland on Ubuntu 26.04).
    "rofi": {"type": "apt", "packages": ["rofi"]},
    # Gaming overlays: use host APT packages so GOverlay can configure the
    # native MangoHud installation used by Steam/Proton games.
    "mangohud": {"type": "apt", "packages": ["mangohud"]},
    "goverlay": {"type": "apt", "packages": ["goverlay"]},
    # Optional companion tools and GOverlay preview utilities. Ubuntu's
    # goverlay package pulls in its hard dependencies automatically, but
    # these aliases also permit standalone installs in Self Lock.
    "vkbasalt": {"type": "apt", "packages": ["vkbasalt"]},
    "gamemode": {"type": "apt", "packages": ["gamemode"]},
    "mesa-utils": {"type": "apt", "packages": ["mesa-utils"]},
    "vulkan-tools": {"type": "apt", "packages": ["vulkan-tools"]},
    "mangohudctl": {"type": "apt", "packages": ["mangohudctl"]},
    "mangoplot": {"type": "apt", "packages": ["mangoplot"]},
}


PACKAGE_BUNDLES = {
    "coding": [
        "pipx",
        "git",
        "curl",
        "build-essential",
        "cmake",
        "ninja",
        "python",
        "qt6",
        "pyqt6",
        "vscode",
        "neovim",
        "wl-clipboard",
        "clang",
    ],
}
