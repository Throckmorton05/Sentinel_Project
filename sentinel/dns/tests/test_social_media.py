"""Social sources work offline without turning YouTube into a category block."""
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from sentinel.dns import generator, social_media as social
from sentinel.dns.categories import SOCIAL_MEDIA_SOURCES
from sentinel.dns.dns_parser import parse_blocklist


@pytest.fixture
def category(tmp_path, monkeypatch):
    path = tmp_path / 'social_media.txt'
    monkeypatch.setattr(generator, 'DNS_DATA_DIR', tmp_path)
    monkeypatch.setattr(generator, 'CATEGORY_FILES', {'social_media': path})
    monkeypatch.setattr(social, 'read_protected', lambda path: Path(path).read_text())
    monkeypatch.setattr(generator, 'atomic_write', lambda path, text: Path(path).write_text(text))
    return path


def settings(enabled=True):
    return {'enabled': True, 'social_media': enabled, 'safe_browsing': True}


@pytest.mark.parametrize('previous', [None, '', '! empty category\n'])
def test_missing_or_empty_category_uses_real_packaged_data(category, previous):
    if previous is not None:
        category.write_text(previous)
    merged, rules = generator.render_dns(settings(), set(), set())
    domains = parse_blocklist(merged)
    assert {'facebook.com', 'bereal.com', 'bere.al', 'instagram.com', 'tiktok.com',
            'x.com', 'reddit.com', 'snapchat.com', 'pinterest.com', 'threads.com'} <= domains
    assert '||facebook.com^' in rules
    assert 'youtube.com' not in domains and 'googlevideo.com' not in domains
    assert 'restrictmoderate.youtube.com' in rules


def test_disabled_category_stays_off(category):
    merged, rules = generator.render_dns(settings(False), set(), set())
    assert 'facebook.com' not in parse_blocklist(merged)
    assert '||facebook.com^' not in rules


def test_latest_category_does_not_reintroduce_removed_snapshot_domains(category):
    category.write_text('new-social.example\n')
    domains = social.load_social_domains(category)
    assert 'new-social.example' in domains and social.CORE_SOCIAL_DOMAINS <= domains
    assert 'reddit.com' not in domains


@pytest.mark.parametrize('domain', social.YOUTUBE_DOMAINS + (
    'www.youtube.com', 'm.youtube.com', 'rr1.googlevideo.com',
    'youtube.googleapis.com', 'googleapis.com', 'google.com',
))
def test_existing_runtime_social_lists_exclude_youtube_and_ancestor_rules(category, domain):
    category.write_text(f'facebook.com\n{domain}\n')
    domains = parse_blocklist(generator.render_dns(settings(), set(), set())[0])
    assert domain not in domains and 'facebook.com' in domains


def test_explicit_youtube_block_and_social_allow_exceptions_still_work(category):
    merged, rules = generator.render_dns(settings(), {'youtube.com'}, {'facebook.com'})
    domains = parse_blocklist(merged)
    assert 'youtube.com' in domains and 'facebook.com' not in domains
    assert '||youtube.com^$dnsrewrite=NXDOMAIN;;,important' in rules
    assert '@@||facebook.com^' in rules


def test_offline_first_refresh_generates_category_and_reports_retry(category):
    with (patch.object(generator, 'download_source', side_effect=requests.ConnectionError('offline')),
          patch.object(generator, 'load_cached_source_domains', return_value=set())):
        failures = generator.generate_category_blocklist('social_media', SOCIAL_MEDIA_SOURCES)
    domains = parse_blocklist(category.read_text())
    assert 'facebook.com' in domains and 'bereal.com' in domains and 'reddit.com' in domains
    assert len(failures) == 1 and 'offline' in failures[0]


def test_download_and_cache_are_filtered_before_becoming_category(category):
    with (patch.object(generator, 'download_source', return_value='facebook.com\nyoutube.com\ngoogleapis.com\n'),
          patch.object(generator, 'save_cached_source_domains') as save):
        assert generator.generate_category_blocklist('social_media', SOCIAL_MEDIA_SOURCES) == []
    assert save.call_args.args[2] == {'facebook.com'}
    assert 'youtube.com' not in parse_blocklist(category.read_text())
    with (patch.object(generator, 'download_source', side_effect=requests.ConnectionError('offline')),
          patch.object(generator, 'load_cached_source_domains', return_value={'facebook.com', 'youtu.be'})):
        generator.generate_category_blocklist('social_media', SOCIAL_MEDIA_SOURCES)
    assert 'youtu.be' not in parse_blocklist(category.read_text())


def test_unsafe_category_is_not_treated_as_missing_data(category, monkeypatch):
    def unsafe(path):
        raise ValueError('Untrusted category file')
    monkeypatch.setattr(social, 'read_protected', unsafe)
    with pytest.raises(ValueError, match='Untrusted'):
        generator.render_dns(settings(), set(), set())
