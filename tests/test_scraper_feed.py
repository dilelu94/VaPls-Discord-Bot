import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "instagram_scraper"))
import scraper


def _media(pk, code, caption="", media_type=2):
    return {
        "pk": pk,
        "id": pk,
        "code": code,
        "media_type": media_type,
        "caption": {"text": caption},
    }


def test_raw_reels_reads_items_with_ads():
    cl = MagicMock()
    cl.private_request.return_value = {
        "items": [],
        "items_with_ads": [
            {"media": _media("1", "ABC", "hola")},
            {"media": _media("2", "DEF")},
        ],
        "status": "ok",
    }
    reels = scraper._raw_reels(cl, "clips/discover/")
    assert [r["code"] for r in reels] == ["ABC", "DEF"]
    assert reels[0]["caption"] == "hola"
    assert reels[0]["url"] == "https://www.instagram.com/reel/ABC/"
    cl.private_request.assert_called_once_with("clips/discover/", data=" ", params={"max_id": ""})


def test_raw_reels_reads_items_and_dedupes_pk():
    cl = MagicMock()
    cl.private_request.return_value = {
        "items": [{"media": _media("1", "ABC")}],
        "items_with_ads": [{"media": _media("1", "ABC")}],
        "status": "ok",
    }
    reels = scraper._raw_reels(cl, "clips/discover/")
    assert [r["code"] for r in reels] == ["ABC"]


def test_raw_reels_skips_non_video():
    cl = MagicMock()
    cl.private_request.return_value = {
        "items": [{"media": _media("1", "PHOTO", media_type=1)}],
        "items_with_ads": [],
    }
    assert scraper._raw_reels(cl, "clips/discover/") == []


def test_raw_reels_handles_error():
    cl = MagicMock()
    cl.private_request.side_effect = Exception("boom")
    assert scraper._raw_reels(cl, "clips/discover/") == []


def test_fetch_feed_reels_connected_first():
    cl = MagicMock()
    cl.private_request.return_value = {
        "items": [{"media": _media("1", "ALGO")}],
        "items_with_ads": [],
    }
    reels = scraper.fetch_feed_reels(cl)
    assert [r["code"] for r in reels] == ["ALGO"]
    cl.private_request.assert_called_once_with("clips/connected/", data=" ", params={"max_id": ""})


def test_fetch_feed_reels_falls_back_to_explore():
    cl = MagicMock()

    def fake_request(endpoint, **kw):
        if endpoint == "clips/connected/":
            return {"items": [], "items_with_ads": [], "status": "ok"}
        return {"items": [{"media": _media("1", "EXPLORE")}], "items_with_ads": [], "status": "ok"}

    cl.private_request.side_effect = fake_request
    reels = scraper.fetch_feed_reels(cl)
    assert [r["code"] for r in reels] == ["EXPLORE"]


def test_fetch_feed_reels_empty():
    cl = MagicMock()
    cl.private_request.return_value = {"items": [], "items_with_ads": [], "status": "ok"}
    assert scraper.fetch_feed_reels(cl) == []


@patch("scraper.requests.post")
@patch("scraper.load_seen_reels")
@patch("scraper.save_seen_reels")
def test_push_home_feed_dedupes_seen(mock_save, mock_load, mock_post):
    mock_load.return_value = set()
    mock_post.return_value = MagicMock(status_code=200, json=lambda: {"added": 2, "total": 2})
    cl = MagicMock()
    with patch("scraper.fetch_feed_reels", return_value=[
        {"code": "AAA", "url": "u", "caption": ""},
        {"code": "BBB", "url": "u", "caption": ""},
    ]):
        scraper.push_home_feed(cl)
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["reels"][0]["code"] == "AAA"
    saved = mock_save.call_args.args[0]
    assert saved == {"AAA", "BBB"}

    mock_post.reset_mock()
    mock_load.return_value = {"AAA", "BBB"}
    with patch("scraper.fetch_feed_reels", return_value=[
        {"code": "AAA", "url": "u", "caption": ""},
    ]):
        scraper.push_home_feed(cl)
    mock_post.assert_not_called()


@patch("scraper.requests.post")
@patch("scraper.load_seen_reels")
@patch("scraper.save_seen_reels")
def test_push_home_feed_skips_save_on_error(mock_save, mock_load, mock_post):
    mock_load.return_value = set()
    mock_post.return_value = MagicMock(status_code=500)
    cl = MagicMock()
    with patch("scraper.fetch_feed_reels", return_value=[
        {"code": "AAA", "url": "u", "caption": ""},
    ]):
        scraper.push_home_feed(cl)
    mock_post.assert_called_once()
    mock_save.assert_not_called()
