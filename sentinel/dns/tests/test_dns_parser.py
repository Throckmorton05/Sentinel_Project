from sentinel.dns.dns_parser import normalize_domain, parse_blocklist


def test_normalize_plain_domain():
    assert normalize_domain("example.com") == "example.com"


def test_normalize_uppercase():
    assert normalize_domain("Example.COM") == "example.com"


def test_normalize_www():
    assert normalize_domain("www.example.com") == "example.com"


def test_normalize_trailing_dot():
    assert normalize_domain("example.com.") == "example.com"


def test_normalize_url():
    assert normalize_domain("https://example.com/some/path") == "example.com"


def test_normalize_adblock_rule():
    assert normalize_domain("||example.com^") == "example.com"


def test_normalize_adblock_rule_with_modifier():
    assert normalize_domain("||example.com^$important") == "example.com"


def test_reject_ip_addresses():
    assert normalize_domain("127.0.0.1") is None
    assert normalize_domain("0.0.0.0") is None
    assert normalize_domain("::1") is None


def test_reject_localhost():
    assert normalize_domain("localhost") is None
    assert normalize_domain("localhost.localdomain") is None


def test_reject_invalid_domains():
    assert normalize_domain("garbage") is None
    assert normalize_domain("not a domain") is None
    assert normalize_domain("-example.com") is None
    assert normalize_domain("example-.com") is None


def test_parse_plain_domains():
    text = """
    example.com
    example.org
    """

    assert parse_blocklist(text) == {
        "example.com",
        "example.org",
    }


def test_parse_hosts_format():
    text = """
    0.0.0.0 example.com
    127.0.0.1 example.org
    """

    assert parse_blocklist(text) == {
        "example.com",
        "example.org",
    }


def test_parse_adblock_format():
    text = """
    ||example.com^
    ||example.org^$important
    """

    assert parse_blocklist(text) == {
        "example.com",
        "example.org",
    }


def test_parse_urls():
    text = """
    https://example.com/path
    http://example.org/other/path
    """

    assert parse_blocklist(text) == {
        "example.com",
        "example.org",
    }


def test_ignore_comments():
    text = """
    # hosts comment
    ! adblock comment
    example.com
    """

    assert parse_blocklist(text) == {"example.com"}


def test_ignore_adblock_exception_rules():
    text = """
    @@||allowed.example.com^
    ||blocked.example.com^
    """

    assert parse_blocklist(text) == {"blocked.example.com"}


def test_deduplicates_equivalent_domains():
    text = """
    example.com
    www.example.com
    Example.COM
    example.com.
    0.0.0.0 example.com
    ||example.com^
    """

    assert parse_blocklist(text) == {"example.com"}


def test_reject_garbage():
    text = """
    garbage
    127.0.0.1
    localhost
    this is not a blocklist entry
    """

    assert parse_blocklist(text) == set()
