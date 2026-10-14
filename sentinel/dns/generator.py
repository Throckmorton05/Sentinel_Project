from sentinel.browser_policies.restrictions import NON_GOOGLE_SEARCH_DOMAINS
from sentinel.dns.safe_search import SAFE_SEARCH_REWRITES, matches_domain
from sentinel.dns.downloader import download_source
from sentinel.dns.dns_parser import parse_blocklist
from sentinel.dns.models import BlocklistSource
from sentinel.dns.social_media import (
    CORE_SOCIAL_DOMAINS, bundled_social_domains, filter_social_domains, load_social_domains,
)

from sentinel.dns.categories import (
    ADULT_SOURCES,
    BROWSERS_SANS_SAFESEARCH_SOURCES,
    GAMBLING_SOURCES,
    SOCIAL_MEDIA_SOURCES,
)

from sentinel.dns.storage import atomic_write, read_protected

from sentinel.settings.paths import (
    DNS_DATA_DIR,
    DNS_CACHE_DIR,
    ADULT_HEAVY_SITES_FILE,
    UNSAFE_SEARCH_ENGINES_FILE,
)


def _source_cache_name(source: BlocklistSource) -> str:
    """
    Generate a filesystem-safe cache name for a blocklist source.
    """

    name = source.name.lower()

    safe_name = "".join(char if char.isalnum() else "_" for char in name)

    return safe_name.strip("_")


def load_cached_source_domains(
    category_name: str,
    source: BlocklistSource,
) -> set[str]:

    cache_dir = DNS_CACHE_DIR / category_name
    cache_path = cache_dir / f"{_source_cache_name(source)}.txt"

    if not cache_path.exists():
        return set()

    return parse_blocklist(read_protected(cache_path))


def save_cached_source_domains(
    category_name: str,
    source: BlocklistSource,
    domains: set[str],
) -> None:

    cache_dir = DNS_CACHE_DIR / category_name

    cache_path = cache_dir / f"{_source_cache_name(source)}.txt"

    atomic_write(cache_path, "".join(f"{domain}\n" for domain in sorted(domains)))


def generate_category_blocklist(
    category_name: str,
    sources: list[BlocklistSource],
) -> list[str]:

    domains: set[str] = set(CORE_SOCIAL_DOMAINS) if category_name == 'social_media' else set()
    failures: list[str] = []

    for source in sources:
        try:
            text = download_source(source)
            source_domains = parse_blocklist(text)
            if category_name == 'social_media':
                source_domains = filter_social_domains(source_domains)

            if not source_domains:
                raise ValueError(f"{source.name} returned no valid domains")

            save_cached_source_domains(
                category_name,
                source,
                source_domains,
            )

            domains.update(source_domains)

            print(f"[OK] {source.name}: {len(source_domains)} domains")

        except Exception as e:
            print(f"[ERROR] Failed to download {source.name}: {e}")

            cached_domains = load_cached_source_domains(
                category_name,
                source,
            )
            if category_name == 'social_media':
                cached_domains = filter_social_domains(cached_domains)
                if not cached_domains:
                    cached_domains = bundled_social_domains()

            if cached_domains:
                failures.append(f"{source.name}: {e}")
                domains.update(cached_domains)

                print(
                    f"[CACHE] {source.name}: using {len(cached_domains)} cached domains"
                )
            else:
                raise ValueError(
                    f"{source.name}: no cached copy available; retaining the previous category file"
                ) from e

    output_path = DNS_DATA_DIR / f"{category_name}.txt"

    if not domains:
        raise ValueError(f"{category_name} has no valid domains; retaining the previous file")
    atomic_write(output_path, "".join(f"{domain}\n" for domain in sorted(domains)))

    print(f"Generated {output_path}")
    print(f"Found {len(domains)} total unique domains")
    return failures


CATEGORY_FILES = {
    "adult": DNS_DATA_DIR / "adult.txt",
    "gambling": DNS_DATA_DIR / "gambling.txt",
    "social_media": DNS_DATA_DIR / "social_media.txt",
    "browsers_sans_safe_search": DNS_DATA_DIR / "browsers_sans_safesearch.txt",
    "adult_heavy_sites": ADULT_HEAVY_SITES_FILE,
    "unsafe_search_engines": UNSAFE_SEARCH_ENGINES_FILE,
}


def render_dns(settings: dict[str, bool], blocked: set[str], allowed: set[str]) -> tuple[str, str]:
    """Render an explicit daemon snapshot; never interpret settings.toml here."""
    categories: set[str] = set()
    if settings["enabled"]:
        for name, path in CATEGORY_FILES.items():
            if settings[name]:
                # Missing/empty active sources must not silently remove protection.
                domains = (load_social_domains(path) if name == 'social_media'
                           else parse_blocklist(read_protected(path)))
                if not domains:
                    raise ValueError(f"Enabled DNS source {path} contains no valid domains.")
                categories.update(domains)
    else:
        # The master switch controls optional categories/custom lists. The
        # Chrome/Google platform baseline remains in force in every lock mode.
        blocked, allowed = set(), set()
    baseline = set(NON_GOOGLE_SEARCH_DOMAINS)
    merged = (categories - allowed) | blocked | baseline
    merged_text = "".join(f"{domain}\n" for domain in sorted(merged))
    rules = [f"||{domain}^\n" for domain in sorted(categories - blocked - baseline)]
    rules.extend(f"@@||{domain}^\n" for domain in sorted(allowed))
    rules.extend(f"||{domain}^$important\n" for domain in sorted(blocked))
    # DNS rewrites outrank ordinary block/allow rules. NXDOMAIN also outranks
    # CNAME rewrites, so scheduled and durable blocks cannot become an allow
    # through Sentinel's own (or external) SafeSearch rewrites.
    rules.extend(f"||{domain}^$dnsrewrite=NXDOMAIN;;,important\n"
                 for domain in sorted(blocked | baseline))
    if settings.get("safe_browsing", True):
        explicit_domains = blocked | baseline
        for host, target in sorted(SAFE_SEARCH_REWRITES.items()):
            # A few suffix lookups per host, not a scan over the hundreds of
            # thousands of category domains on every reconciliation.
            labels = host.split(".")
            suffixes = {".".join(labels[index:]) for index in range(len(labels))}
            explicit_block = bool(suffixes & explicit_domains)
            category_block = bool(suffixes & categories)
            exception = bool(suffixes & allowed)
            if explicit_block:
                continue  # The broad NXDOMAIN block already covers this host.
            if category_block and not exception:
                # Keep category blocks effective even against external rewrites.
                rules.append(f"|{host}|$dnsrewrite=NXDOMAIN;;,important\n")
            else:
                rules.append(f"|{host}|$dnsrewrite=NOERROR;CNAME;{target},important\n")
    return merged_text, "".join(rules)


def browser_dns_checks(rules: str) -> dict[str, str]:
    """Expected rule matches for representative search and all YouTube hosts."""
    checks = {}
    lines = [rule for rule in rules.splitlines() if "$dnsrewrite=" in rule]
    for host in ("www.google.com", "www.google.co.uk", "bing.com", "duckduckgo.com",
                 "www.youtube.com", "m.youtube.com", "youtubei.googleapis.com",
                 "youtube.googleapis.com", "www.youtube-nocookie.com"):
        candidates = []
        for rule in lines:
            mask = rule.split("$", 1)[0]
            if mask.startswith("||"):
                match = matches_domain(host, mask[2:].removesuffix("^"))
            else:
                match = mask == f"|{host}|"
            if match:
                candidates.append(rule)
        if candidates:
            # An NXDOMAIN block outranks every CNAME rewrite for this host.
            blocks = [rule for rule in candidates if "=NXDOMAIN;;" in rule]
            checks[host] = max(blocks, key=len) if blocks else candidates[0]
    return checks


def refresh_category_sources() -> list[str]:
    """Only trusted packaged URLs; no client-supplied paths or sources."""
    failures: list[str] = []
    for name, sources in (
        ("adult", ADULT_SOURCES), ("gambling", GAMBLING_SOURCES),
        ("social_media", SOCIAL_MEDIA_SOURCES),
        ("browsers_sans_safesearch", BROWSERS_SANS_SAFESEARCH_SOURCES),
    ):
        failures.extend(generate_category_blocklist(name, sources))
    return failures


if __name__ == "__main__":
    # Legacy command is a daemon client as well; it cannot bypass policy.
    from sentinel.daemon.client import regenerate_dns
    print(regenerate_dns(update_sources=True))
