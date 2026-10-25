"""The shared Wikipedia API helper and HTTP GET retries, offline (canned replies, no waiting)."""
import pytest
import requests
from conftest import Reply
from refs import check_edits, config, scan

OK = {"query": {"pages": []}}
LAG = {"error": {"code": "maxlag", "info": "Waiting for a database server"}}


def test_retries_a_network_error_then_answers(http):
    fake = http(requests.ConnectionError("reset"), Reply(200, OK))
    assert config.wiki_api(dict(action="query")) == OK and len(fake.calls) == 2


def test_other_api_errors_fail_at_once_with_their_code(http):
    fake = http(Reply(200, {"error": {"code": "assertuserfailed", "info": "You are no longer logged in."}}))
    with pytest.raises(config.ApiError) as e:
        config.wiki_api(dict(action="query"))
    assert e.value.code == "assertuserfailed" and len(fake.calls) == 1


def test_one_try_for_cosmetic_lookups(http):
    fake = http(Reply(503))
    with pytest.raises(config.WikiUnavailable, match="HTTP 503, 1 try"):
        config.wiki_api(dict(action="query"), tries=1)
    assert len(fake.calls) == 1


def test_posts_go_as_form_data_to_the_url_given(http):
    fake = http(Reply(200, OK))
    config.wiki_api(dict(action="wbgetentities"), post=True, url="https://www.wikidata.org/w/api.php")
    method, url, kw = fake.calls[0]
    assert method == "post" and url == "https://www.wikidata.org/w/api.php" and kw["data"] == {"action": "wbgetentities"}


def test_lagged_wikipedia_is_an_error_not_missing_pages(http):
    # check-edits used to read a maxlag reply as "no pages" and report every page as missing
    http(Reply(200, LAG), Reply(200, LAG), Reply(200, LAG))
    with pytest.raises(config.WikiUnavailable, match="maxlag"):
        scan.fetch_wikitext([7])
    http(Reply(200, LAG), Reply(200, LAG), Reply(200, LAG))
    with pytest.raises(config.WikiUnavailable):
        check_edits.reverted_revids([101])


def test_http_get_keeps_its_timeout_and_caps_retry_after(http, monkeypatch):
    waits = []
    monkeypatch.setattr(config.time, "sleep", waits.append)
    fake = http(requests.Timeout("slow"), Reply(429, headers={"retry-after": "3600"}), Reply(200, OK))
    assert config.http_get("https://news.example.com/x", gap=0, timeout=10).status_code == 200
    assert [kw["timeout"] for _, _, kw in fake.calls] == [10, 10, 10] and waits == [1, 60]
