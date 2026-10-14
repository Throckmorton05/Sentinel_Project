# sentinel/settings/info_loader.py

import tomllib

from sentinel.settings.paths import ADGUARD_CREDENTIALS_FILE
from sentinel.dns.storage import read_protected


def load_adguard_info():
    """The DNS control credentials are private daemon configuration."""
    return tomllib.loads(read_protected(ADGUARD_CREDENTIALS_FILE, private=True))


def adguard_url():

    info = load_adguard_info()

    return info.get("adguard", {}).get(
        "url",
        "http://127.0.0.1",
    )


def adguard_username():

    info = load_adguard_info()

    return info.get("adguard", {}).get(
        "username",
        "",
    )


def adguard_password():

    info = load_adguard_info()

    return info.get("adguard", {}).get(
        "password",
        "",
    )
