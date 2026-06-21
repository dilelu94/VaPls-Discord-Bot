"""Unit tests for IPTV channel parsing, caching, and classification."""

import pytest
from unittest.mock import patch
import iptv


def test_channel_defaults():
    ch = iptv.Channel()
    assert ch.name == ""
    assert ch.url == ""
    assert ch.tvg_id == ""
    assert ch.tvg_logo == ""
    assert ch.group == ""
    assert ch.language == "other"


def test_parse_m3u():
    sample_m3u = """#EXTM3U
#EXTINF:-1 tvg-id="TestID1" tvg-name="Test Name 1" group-title="Sports",Test 1
http://example.com/test1.m3u8
#EXTINF:-1 tvg-id="TestID2" group-title="News",Test 2
http://example.com/test2.m3u8
"""
    channels = iptv._parse_m3u(sample_m3u)
    assert len(channels) == 2

    assert channels[0].tvg_id == "TestID1"
    assert channels[0].name == "Test Name 1"
    assert channels[0].group == "Sports"
    assert channels[0].url == "http://example.com/test1.m3u8"

    assert channels[1].tvg_id == "TestID2"
    assert channels[1].name == "Test 2"
    assert channels[1].group == "News"
    assert channels[1].url == "http://example.com/test2.m3u8"


@pytest.mark.asyncio
async def test_ensure_cache_classification():
    main_m3u = """#EXTM3U
#EXTINF:-1 tvg-id="id-es" tvg-name="Canal ES" group-title="Sports",Canal ES
http://example.com/es.m3u8
#EXTINF:-1 tvg-id="id-en" tvg-name="Canal EN" group-title="News",Canal EN
http://example.com/en.m3u8
#EXTINF:-1 tvg-id="id-other" tvg-name="Canal Other" group-title="Music",Canal Other
http://example.com/other.m3u8
"""
    spa_m3u = """#EXTM3U
#EXTINF:-1 tvg-id="id-es" tvg-name="Canal ES" group-title="Sports",Canal ES
http://example.com/es.m3u8
"""
    eng_m3u = """#EXTM3U
#EXTINF:-1 tvg-id="id-en" tvg-name="Canal EN" group-title="News",Canal EN
http://example.com/en.m3u8
"""

    async def mock_fetch_and_cache(session, url, path):
        if "spa.m3u" in url:
            return spa_m3u
        elif "eng.m3u" in url:
            return eng_m3u
        else:
            return main_m3u

    # Clear memory cache first
    iptv._cached = []
    iptv._cache_ts = 0

    with patch("iptv._fetch_and_cache", side_effect=mock_fetch_and_cache):
        channels = await iptv.get_all_channels()
        assert len(channels) == 3

        # Check classification
        ch_es = next(c for c in channels if c.tvg_id == "id-es")
        assert ch_es.language == "es"

        ch_en = next(c for c in channels if c.tvg_id == "id-en")
        assert ch_en.language == "en"

        ch_other = next(c for c in channels if c.tvg_id == "id-other")
        assert ch_other.language == "other"


@pytest.mark.asyncio
async def test_search_view_filtering():
    from bot import IptvSearchView
    import discord
    from unittest.mock import MagicMock

    # Create dummy channels
    ch1 = iptv.Channel()
    ch1.name = "A Sports ES"
    ch1.group = "Sports"
    ch1.language = "es"

    ch2 = iptv.Channel()
    ch2.name = "B News EN"
    ch2.group = "News"
    ch2.language = "en"

    ch3 = iptv.Channel()
    ch3.name = "C Music Other"
    ch3.group = "Music"
    ch3.language = "other"

    channels = [ch1, ch2, ch3]
    mock_voice_channel = MagicMock(spec=discord.VoiceChannel)

    # Test filtering by Spanish language, any category
    view = IptvSearchView(channels, mock_voice_channel)
    view.selected_language = "es"
    view.selected_category = "all"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 1
    assert filtered[0].name == "A Sports ES"

    # Test filtering by English language, News category
    view.selected_language = "en"
    view.selected_category = "News"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 1
    assert filtered[0].name == "B News EN"

    # Test filtering by English language, Sports category (should be empty)
    view.selected_language = "en"
    view.selected_category = "Sports"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 0

    # Test filtering by 'all' language, 'all' category
    view.selected_language = "all"
    view.selected_category = "all"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 3
    # Check that sorting is case-insensitive alphabetical
    assert filtered[0].name == "A Sports ES"
    assert filtered[1].name == "B News EN"
    assert filtered[2].name == "C Music Other"


@pytest.mark.asyncio
async def test_search_view_pagination():
    """Pagination splits channels into pages of PAGE_SIZE and clamps bounds."""
    from bot import IptvSearchView
    import discord
    from unittest.mock import MagicMock

    # Create 30 channels (more than PAGE_SIZE=25)
    channels = []
    for i in range(30):
        ch = iptv.Channel()
        ch.name = f"Channel {i:02d}"
        ch.group = "Sports"
        ch.language = "es"
        channels.append(ch)

    mock_vc = MagicMock(spec=discord.VoiceChannel)
    view = IptvSearchView(channels, mock_vc)
    view.selected_language = "es"
    view.selected_category = "all"

    filtered = view.get_filtered_channels()
    assert len(filtered) == 30

    # Page 0 should show first 25
    assert view.current_page == 0
    assert view._total_pages(len(filtered)) == 2

    # Navigate to page 1
    view.current_page = 1
    start = view.current_page * view.PAGE_SIZE
    page_channels = filtered[start : start + view.PAGE_SIZE]
    assert len(page_channels) == 5  # remaining 5

    # Clamping: page beyond max goes to last page
    view.current_page = 99
    view.setup_components()
    assert view.current_page == 1  # clamped to last valid page

    # Clamping: negative page goes to 0
    view.current_page = -1
    view.setup_components()
    assert view.current_page == 0


@pytest.mark.asyncio
async def test_search_view_text_search():
    """Free-text search filters channels by substring match."""
    from bot import IptvSearchView
    import discord
    from unittest.mock import MagicMock

    ch1 = iptv.Channel()
    ch1.name = "ESPN Deportes"
    ch1.group = "Sports"
    ch1.language = "es"

    ch2 = iptv.Channel()
    ch2.name = "Fox Sports"
    ch2.group = "Sports"
    ch2.language = "es"

    ch3 = iptv.Channel()
    ch3.name = "CNN en Español"
    ch3.group = "News"
    ch3.language = "es"

    channels = [ch1, ch2, ch3]
    mock_vc = MagicMock(spec=discord.VoiceChannel)
    view = IptvSearchView(channels, mock_vc)
    view.selected_language = "es"
    view.selected_category = "all"

    # Search for "espn"
    view.search_query = "espn"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 1
    assert filtered[0].name == "ESPN Deportes"

    # Search for "sports" matches Fox Sports only (ESPN has "Deportes")
    view.search_query = "sports"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 1
    assert filtered[0].name == "Fox Sports"

    # Search is case-insensitive
    view.search_query = "CNN"
    filtered = view.get_filtered_channels()
    assert len(filtered) == 1
    assert filtered[0].name == "CNN en Español"

    # Clear search shows all again
    view.search_query = None
    filtered = view.get_filtered_channels()
    assert len(filtered) == 3

