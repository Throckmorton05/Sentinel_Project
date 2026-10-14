from sentinel.dns.models import BlocklistSource


ADULT_SOURCES = [
    BlocklistSource(
        name="HaGeZi NSFW",
        url="https://gitlab.com/hagezi/mirror/-/raw/main/dns-blocklists/adblock/nsfw.txt",
        category="adult",
    ),
    BlocklistSource(
        name="StevenBlack Porn",
        url="https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn-only/hosts",
        category="adult",
    ),
]

GAMBLING_SOURCES = [
    BlocklistSource(
        name="HaGeZi Gambling",
        url="https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/gambling.txt",
        category="gambling",
    ),
    BlocklistSource(
        name="StevenBlack Gambling",
        url="https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/gambling-only/hosts",
        category="gambling",
    ),
]

SOCIAL_MEDIA_SOURCES = [
    BlocklistSource(
        name="HaGeZi Social",
        url="https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/social.txt",
        category="social_media",
    ),
]

BROWSERS_SANS_SAFESEARCH_SOURCES = [
    BlocklistSource(
        name="HaGeZi No SafeSearch",
        url="https://gitlab.com/hagezi/mirror/-/raw/main/dns-blocklists/adblock/nosafesearch.txt",
        category="browsers_sans_safesearch",
    ),
]
