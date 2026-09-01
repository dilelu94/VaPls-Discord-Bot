import pytest
from unittest.mock import AsyncMock, patch
import jkanime


def test_is_jkanime_url():
    assert jkanime.is_jkanime_url("https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/1/") is True
    assert jkanime.is_jkanime_url("http://jkanime.net/chainsaw-man/") is True
    assert jkanime.is_jkanime_url("https://youtube.com/watch?v=123") is False
    assert jkanime.is_jkanime_url("jojo stone ocean") is False
    assert jkanime.is_jkanime_url(None) is False


@pytest.mark.asyncio
async def test_search_jkanime():
    mock_html = """
    <html>
      <body>
        <a href="https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/">
          <h5>JoJo no Kimyou na Bouken Part 6: Stone Ocean</h5>
        </a>
        <a href="https://jkanime.net/jojo-no-kimyou-na-bouken-ougon-no-kaze/">
          <h5>JoJo no Kimyou na Bouken: Ougon no Kaze</h5>
        </a>
      </body>
    </html>
    """
    with patch.object(jkanime, "_fetch", AsyncMock(return_value=mock_html)):
        results = await jkanime.search_jkanime("jojo")
        assert len(results) == 2
        assert results[0]["slug"] == "jojo-no-kimyou-na-bouken-part-6-stone-ocean"
        assert results[0]["title"] == "JoJo no Kimyou na Bouken Part 6: Stone Ocean"
        assert results[1]["slug"] == "jojo-no-kimyou-na-bouken-ougon-no-kaze"


@pytest.mark.asyncio
async def test_search_jkanime_empty():
    with patch.object(jkanime, "_fetch", AsyncMock(return_value="")):
        results = await jkanime.search_jkanime("nonexistent")
        assert results == []


@pytest.mark.asyncio
async def test_get_jkanime_anime_info():
    mock_html = """
    <html>
      <body>
        <h1>JoJo no Kimyou na Bouken Part 6: Stone Ocean</h1>
        <li><span>Episodios:</span> 12</li>
      </body>
    </html>
    """
    with patch.object(jkanime, "_fetch", AsyncMock(return_value=mock_html)):
        info = await jkanime.get_jkanime_anime_info("jojo-no-kimyou-na-bouken-part-6-stone-ocean")
        assert info["slug"] == "jojo-no-kimyou-na-bouken-part-6-stone-ocean"
        assert info["title"] == "JoJo no Kimyou na Bouken Part 6: Stone Ocean"
        assert info["episodes"] == 12


@pytest.mark.asyncio
async def test_extract_jkanime_stream():
    episode_html = """
    <html>
      <body>
        <h1>JoJo Stone Ocean - Episodio 1</h1>
        <iframe src="https://jkanime.net/jkplayer/um?e=test_hash"></iframe>
      </body>
    </html>
    """
    player_html = """
    <html>
      <script>
        var streamUrl = "https://nika.playmudos.com/test_stream.m3u8?token=123";
      </script>
    </html>
    """

    async def mock_fetch(session, url, referer=None):
        if "jkplayer" in url:
            return player_html
        return episode_html

    with patch.object(jkanime, "_fetch", side_effect=mock_fetch):
        stream_url, ep_title = await jkanime.extract_jkanime_stream(
            "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/1/"
        )
        assert stream_url == "https://nika.playmudos.com/test_stream.m3u8?token=123"
        assert ep_title == "JoJo Stone Ocean - Episodio 1"


@pytest.mark.asyncio
async def test_extract_jkanime_stream_no_h1():
    episode_html = """
    <html>
      <body>
        <iframe src="https://jkanime.net/jkplayer/um?e=test_hash"></iframe>
      </body>
    </html>
    """
    player_html = """
    <html>
      <script>
        var streamUrl = "https://nika.playmudos.com/test_stream.m3u8?token=123";
      </script>
    </html>
    """

    async def mock_fetch(session, url, referer=None):
        if "jkplayer" in url:
            return player_html
        return episode_html

    with patch.object(jkanime, "_fetch", side_effect=mock_fetch):
        stream_url, ep_title = await jkanime.extract_jkanime_stream(
            "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/1/"
        )
        assert stream_url == "https://nika.playmudos.com/test_stream.m3u8?token=123"
        assert ep_title == "Jojo No Kimyou Na Bouken Part 6 Stone Ocean - Episodio 1"


def test_parse_anime_query():
    assert jkanime.parse_anime_query("jojo part 6 cap 13") == ("jojo part 6", 13)
    assert jkanime.parse_anime_query("anime: naruto 22") == ("naruto", 22)
    assert jkanime.parse_anime_query("dragon ball ep 5") == ("dragon ball", 5)
    assert jkanime.parse_anime_query("jojo part 6") == ("jojo part 6", None)


@pytest.mark.asyncio
async def test_resolve_anime_and_episode_cumulative():
    part1 = {
        "title": "JoJo no Kimyou na Bouken Part 6: Stone Ocean",
        "slug": "jojo-no-kimyou-na-bouken-part-6-stone-ocean",
        "url": "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/",
    }
    part2 = {
        "title": "JoJo no Kimyou na Bouken Part 6: Stone Ocean Part 2",
        "slug": "jojo-no-kimyou-na-bouken-part-6-stone-ocean-part-2",
        "url": "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean-part-2/",
    }

    async def mock_search(query, session=None):
        return [part1, part2]

    async def mock_info(slug, session=None):
        return {"slug": slug, "title": slug, "episodes": 12}

    with patch.object(jkanime, "search_jkanime", side_effect=mock_search), patch.object(
        jkanime, "get_jkanime_anime_info", side_effect=mock_info
    ):
        url, results, rel_ep = await jkanime.resolve_anime_and_episode("jojo part 6 cap 13")
        assert url == "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean-part-2/1/"
        assert rel_ep == 1


