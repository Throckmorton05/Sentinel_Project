import requests

from sentinel.dns.models import BlocklistSource


def download_source(source: BlocklistSource) -> str:
    """
    Download a blocklist and return its raw text.
    """

    response = requests.get(source.url, timeout=30)

    response.raise_for_status()

    return response.text
