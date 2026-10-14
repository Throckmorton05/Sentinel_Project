from pathlib import Path

from .profile_generator import generate_block_profile
from sentinel.settings.paths import PROFILE_DIR


def executable_to_profile_name(executable: str) -> str:
    """
    /usr/bin/kate -> usr.bin.kate
    """
    return executable.lstrip("/").replace("/", ".")


def block_app(executable: str) -> Path:

    #    Generate a blocking AppArmor profile for an executable.
    #    Returns the path of the generated profile.

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    profile_path = get_profile_path(executable)

    profile_text = generate_block_profile(executable)

    profile_path.write_text(profile_text)

    return profile_path


def get_profile_path(executable: str) -> Path:
    profile_name = executable_to_profile_name(executable)
    return PROFILE_DIR / profile_name
