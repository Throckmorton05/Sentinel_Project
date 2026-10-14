"""Commission marketplaces are optional category blocks, including subdomains."""
from pathlib import Path
from unittest.mock import patch

from sentinel.dns import generator
from sentinel.dns.dns_parser import parse_blocklist

SITES = {'fiverr.com', 'vgen.co', 'artistree.io', 'artistsnclients.com', 'skeb.jp', 'commishes.com'}
SOURCE = Path(__file__).resolve().parents[3] / 'configs/dns/adult_heavy_sites.txt'


def test_commission_sites_only_render_with_enabled_category():
    domains = parse_blocklist(SOURCE.read_text())
    assert SITES <= domains
    settings = dict.fromkeys(generator.CATEGORY_FILES, False)
    settings.update(enabled=True, safe_browsing=False)
    with patch.object(generator, 'CATEGORY_FILES', {'adult_heavy_sites': SOURCE}), \
         patch.object(generator, 'read_protected', side_effect=lambda path: path.read_text()):
        _, rules = generator.render_dns(settings, set(), set())
        assert all(site not in rules for site in SITES)
        settings['adult_heavy_sites'] = True
        _, rules = generator.render_dns(settings, set(), set())
        assert all(f'||{site}^\n' in rules for site in SITES)
        settings['enabled'] = False
        _, rules = generator.render_dns(settings, set(), set())
        assert all(site not in rules for site in SITES)
