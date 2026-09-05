"""Behavioral tests for torrent search, magnet link resolution, and GoLive torrent relay."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import torrent_search
from torrent_search import (
    TorrentStreamItem,
    is_infohash,
    is_magnet_link,
    is_stremio_resolve_url,
    is_torrent_input,
    parse_torrent_quality,
    resolve_stremio_or_magnet_url,
    search_stremio_torrents,
)


def test_is_magnet_link():
    assert is_magnet_link("magnet:?xt=urn:btih:8c03a030ad101b5d2c5102a5c0845bb41b9f2960") is True
    assert is_magnet_link("http://example.com/stream.m3u8") is False
    assert is_magnet_link("") is False


def test_is_infohash():
    assert is_infohash("8c03a030ad101b5d2c5102a5c0845bb41b9f2960") is True
    assert is_infohash("not_a_hash") is False


def test_is_stremio_resolve_url():
    url = (
        "https://torrentio.strem.fun/resolve/torbox/90f73123-7565-4ae3-b672-aa96bc026c50/"
        "8c03a030ad101b5d2c5102a5c0845bb41b9f2960/"
        "%5BAnime%20Time%5D%20JoJo's%20Bizarre%20Adventure%20Part%206%20-%20Stone%20Ocean%20-%2026.mkv/25/"
        "%5BAnime%20Time%5D%20JoJo's%20Bizarre%20Adventure%20Part%206%20-%20Stone%20Ocean%20-%2026.mkv"
    )
    assert is_stremio_resolve_url(url) is True


def test_resolve_stremio_or_magnet_url_with_stremio_link():
    stremio_url = (
        "https://torrentio.strem.fun/resolve/torbox/90f73123-7565-4ae3-b672-aa96bc026c50/"
        "8c03a030ad101b5d2c5102a5c0845bb41b9f2960/"
        "%5BAnime%20Time%5D%20JoJo's%20Bizarre%20Adventure%20Part%206%20-%20Stone%20Ocean%20-%2026.mkv"
    )
    resolved, title = resolve_stremio_or_magnet_url(stremio_url)
    assert resolved == stremio_url
    assert "JoJo" in title



def test_parse_torrent_quality():
    assert parse_torrent_quality("Avatar.2009.2160p.UHD.mkv") == "4K"
    assert parse_torrent_quality("Matrix.1080p.BluRay.x264") == "1080p"
    assert parse_torrent_quality("Movie.720p.WEB-DL") == "720p"
    assert parse_torrent_quality("Unknown.Release") == "HD"


@pytest.mark.asyncio
async def test_search_stremio_torrents():
    cinemeta_response = json_bytes(
        {
            "metas": [
                {
                    "id": "tt0133093",
                    "name": "The Matrix",
                    "type": "movie",
                    "year": "1999",
                }
            ]
        }
    )
    torrentio_response = json_bytes(
        {
            "streams": [
                {
                    "name": "Torrentio 1080p",
                    "title": "The.Matrix.1999.1080p.BluRay\n👤 150 💾 2.4 GB",
                    "infoHash": "8c03a030ad101b5d2c5102a5c0845bb41b9f2960",
                }
            ]
        }
    )

    def fake_urlopen(req, timeout=5.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if "cinemeta" in url:
            mock_resp.read.return_value = cinemeta_response
        else:
            mock_resp.read.return_value = torrentio_response
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        items = await search_stremio_torrents("The Matrix", limit=5)
        assert len(items) == 1
        assert items[0].title == "The.Matrix.1999.1080p.BluRay"
        assert items[0].seeders == 150
        assert items[0].quality == "1080p"
        assert items[0].infohash == "8c03a030ad101b5d2c5102a5c0845bb41b9f2960"


@pytest.mark.asyncio
async def test_search_stremio_catalog():
    from torrent_search import search_stremio_catalog

    catalog_data = json_bytes({
        "metas": [
            {
                "id": "kitsu:11",
                "name": "Naruto",
                "poster": "https://media.kitsu.app/poster.jpg",
                "year": "2002",
                "description": "Ninja anime",
            }
        ]
    })

    def fake_urlopen(req, timeout=4.0):
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = catalog_data
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        items = await search_stremio_catalog("Naruto", "anime")
        assert len(items) == 1
        assert items[0]["id"] == "kitsu:11"
        assert items[0]["title"] == "Naruto"
        assert items[0]["type"] == "anime"


@pytest.mark.asyncio
async def test_get_stremio_meta_and_streams():
    from torrent_search import get_stremio_meta, get_stremio_streams

    meta_data = json_bytes({
        "meta": {
            "name": "Naruto",
            "imdb_id": "tt0409591",
            "poster": "https://media.kitsu.app/poster.jpg",
            "videos": [
                {
                    "id": "kitsu:11:1",
                    "title": "Enter: Naruto Uzumaki!",
                    "season": 1,
                    "episode": 1,
                }
            ]
        }
    })

    stream_data = json_bytes({
        "streams": [
            {
                "name": "[TB+] Torrentio",
                "title": "Naruto Episode 001 1080p\n👤 500 💾 250 MB",
                "url": "https://torrentio.strem.fun/resolve/torbox/123/456",
            }
        ]
    })

    def fake_urlopen(req, timeout=5.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        if "meta" in url:
            mock_resp.read.return_value = meta_data
        else:
            mock_resp.read.return_value = stream_data
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        meta = await get_stremio_meta("anime", "kitsu:11")
        assert meta["title"] == "Naruto"
        assert len(meta["episodes"]) == 1
        assert meta["episodes"][0]["title"] == "Enter: Naruto Uzumaki!"

        streams = await get_stremio_streams("anime", "kitsu:11", 1, 1, "tt0409591")
        assert len(streams) >= 1
        assert streams[0]["is_direct"] is True
        assert "resolve/torbox" in streams[0]["url"]


def json_bytes(data: dict) -> bytes:
    import json
    return json.dumps(data).encode("utf-8")

