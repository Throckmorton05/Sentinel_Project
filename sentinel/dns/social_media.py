"""A packaged social-network fallback and permanent YouTube exclusions."""
from pathlib import Path

from .dns_parser import parse_blocklist
from .safe_search import matches_domain
from .storage import read_protected

BUNDLED_SOCIAL_FILE = Path(__file__).parent / 'data' / 'social_media.txt'
CORE_SOCIAL_DOMAINS = frozenset({
    'facebook.com', 'instagram.com', 'tiktok.com', 'bereal.com', 'bere.al',
    'threads.com', 'threads.net',
})
# Exclude both descendants and broad ancestor rules, such as googleapis.com,
# which could otherwise block YouTube's API indirectly.
YOUTUBE_DOMAINS = (
    'youtube.com', 'youtu.be', 'yt.be', 'youtube-nocookie.com',
    'ytimg.com', 'googlevideo.com', 'ggpht.com',
    'youtubei.googleapis.com', 'youtube.googleapis.com',
    'youtubeembeddedplayer.googleapis.com', 'youtube-ui.l.google.com',
    'youtubekids.com', 'youtubeeducation.com',
)


def filter_social_domains(domains):
    return {domain for domain in domains if not any(
        matches_domain(domain, youtube) or matches_domain(youtube, domain)
        for youtube in YOUTUBE_DOMAINS)}


def bundled_social_domains():
    # This data ships with the same trusted, installed package as this code.
    return filter_social_domains(parse_blocklist(BUNDLED_SOCIAL_FILE.read_text())
                                 | CORE_SOCIAL_DOMAINS)


def load_social_domains(path):
    try:
        domains = filter_social_domains(parse_blocklist(read_protected(path)))
    except FileNotFoundError:
        domains = set()
    # Prefer the last downloaded list, allowing upstream corrections/removals.
    return (domains or bundled_social_domains()) | CORE_SOCIAL_DOMAINS
