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
    magnet, title = resolve_stremio_or_magnet_url(stremio_url)
    assert magnet == "magnet:?xt=urn:btih:8c03a030ad101b5d2c5102a5c0845bb41b9f2960"
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


def json_bytes(data: dict) -> bytes:
    import json
    return json.dumps(data).encode("utf-8")
