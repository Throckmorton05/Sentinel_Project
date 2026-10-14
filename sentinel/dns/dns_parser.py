import ipaddress
import re
from urllib.parse import urlparse


SKIP_DOMAINS = {
    "localhost",
    "localhost.localdomain",
}

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:"
    r"[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\."
    r")+"
    r"[a-z0-9]"
    r"(?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def normalize_domain(domain: str) -> str | None:
    """
    Normalize and validate a domain.

    Returns the normalized domain, or None if the input is not
    a valid domain that Sentinel should place in its DNS blocklist.
    """

    domain = domain.strip().lower()

    # Handle URLs.
    if domain.startswith(("http://", "https://")):
        parsed = urlparse(domain)
        domain = parsed.hostname or ""

    # Remove common Adblock syntax.
    if domain.startswith("||"):
        domain = domain[2:]

    if "^" in domain:
        domain = domain.split("^", 1)[0]

    # Remove trailing DNS dot.
    domain = domain.rstrip(".")

    # Remove a leading www. so equivalent entries deduplicate.
    if domain.startswith("www."):
        domain = domain[4:]

    if not domain:
        return None

    if domain in SKIP_DOMAINS:
        return None

    # IP addresses should never become domain blocking rules here.
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass

    if not DOMAIN_RE.fullmatch(domain):
        return None

    return domain


def parse_blocklist(text: str) -> set[str]:
    """
    Parse common DNS blocklist formats and return normalized domains.

    Supported formats include:

        example.com
        0.0.0.0 example.com
        127.0.0.1 example.com
        ||example.com^
        ||example.com^$important
        https://example.com/path
    """

    domains: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        # Comments.
        if line.startswith(("#", "!")):
            continue

        candidate: str | None = None

        # Adblock exception rules are allow rules, not block rules.
        if line.startswith("@@"):
            continue

        # Adblock domain rules.
        if line.startswith("||"):
            candidate = line

        else:
            parts = line.split()

            # Hosts-file format:
            # 0.0.0.0 example.com
            # 127.0.0.1 example.com
            if len(parts) >= 2:
                try:
                    ipaddress.ip_address(parts[0])
                    candidate = parts[1]
                except ValueError:
                    # Unknown multi-column format.
                    continue

            # Plain domain or URL.
            elif len(parts) == 1:
                candidate = parts[0]

        if candidate is None:
            continue

        domain = normalize_domain(candidate)

        if domain is not None:
            domains.add(domain)

    return domains
