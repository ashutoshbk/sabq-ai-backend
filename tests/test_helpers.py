import pytest

from app.core.cache import SingleValueTTLCache
from app.core.resources import load_allowed_domains
from app.services.trends_parsing import (
    clean_url,
    coerce_choice,
    collect_citation_urls,
    match_domain,
    parse_cards_json,
    shorten,
    url_key,
)


def test_clean_url_strips_utm_and_fragment():
    assert (
        clean_url("https://Example.com/a?utm_source=openai&x=1#top") == "https://example.com/a?x=1"
    )


def test_clean_url_strips_other_tracking_params_but_keeps_real_ones():
    url = "https://www.weforum.org/a/?trk=article-ssr-frontend-pulse_little-text-block&id=7&fbclid=x"
    assert clean_url(url) == "https://www.weforum.org/a/?id=7"


@pytest.mark.parametrize("bad", ["", None, "not a url", "ftp://x.org/a", "javascript:alert(1)"])
def test_clean_url_rejects_non_http(bad):
    assert clean_url(bad) is None


def test_url_key_ignores_trailing_slash_and_tracking():
    assert url_key("https://a.org/p/?utm_source=openai") == url_key("https://a.org/p")


def test_match_domain_includes_subdomains_but_not_lookalikes():
    domains = load_allowed_domains()
    assert match_domain("https://www.weforum.org/x", domains).name == "World Economic Forum"
    assert match_domain("https://reports.weforum.org/x", domains) is not None
    assert match_domain("https://notweforum.org/x", domains) is None
    assert match_domain("https://weforum.org.evil.com/x", domains) is None


def test_collect_citation_urls_finds_nested_urls():
    dump = {
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"type": "url", "url": "https://a.org/1"}]}},
            {"type": "message", "content": [{"annotations": [{"type": "url_citation", "url": "https://b.org/2?utm_source=openai"}]}]},
        ]
    }
    assert collect_citation_urls(dump) == {"https://a.org/1", "https://b.org/2"}


def test_parse_cards_json_variants():
    assert parse_cards_json('{"trends": [{"title": "a"}]}') == [{"title": "a"}]
    assert parse_cards_json('[{"title": "a"}]') == [{"title": "a"}]
    assert parse_cards_json('Here you go: {"trends": [{"title": "a"}]} Hope it helps') == [{"title": "a"}]
    assert parse_cards_json("nothing useful") == []
    assert parse_cards_json("") == []


def test_coerce_choice():
    assert coerce_choice("policies", ["Policies"], "General") == "Policies"
    assert coerce_choice("zzz", ["Policies"], "General") == "General"


def test_shorten():
    assert shorten("  a   b  ", 10) == "a b"
    assert len(shorten("x" * 100, 20)) == 20


def test_ttl_cache_expires():
    now = [0.0]
    cache = SingleValueTTLCache[str](ttl_seconds=10, clock=lambda: now[0])
    cache.set("v")
    assert cache.get() == "v"
    now[0] = 9.9
    assert cache.get() == "v"
    now[0] = 10.0
    assert cache.get() is None


def test_ttl_cache_zero_ttl_disables_caching():
    cache = SingleValueTTLCache[str](ttl_seconds=0)
    cache.set("v")
    assert cache.get() is None
