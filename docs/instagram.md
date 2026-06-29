# Instagram Reels streaming (GoLive)

The `/instagram` relay endpoint streams Instagram Reels into a Discord voice
channel via GoLive (screen-share video + audio).

## Modes

| Mode     | Trigger                                            | Behavior                                                                                                               |
| -------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **URL**  | `{"url": "https://www.instagram.com/reel/XXXXX/"}` | Extracts that single reel via yt-dlp, plays it once, then stops.                                                       |
| **Feed** | `{}` (no `url`)                                    | Infinite scroll — discovers reel page URLs from the Reels tab feed API, extracts each with yt-dlp, plays back-to-back. |

## Endpoint

```
POST /instagram
X-API-Secret: <RELAY_SECRET>
Content-Type: application/json

{
  "guild_id": 451575911704428554,
  "channel_id": 1089025651786404030,
  "url": "https://www.instagram.com/reel/XXXXX/"   // optional
}
```

**Response (success):**

```json
{
  "started": true,
  "guild_id": 451575911704428554,
  "channel_name": "General",
  "video_ssrc": 21810
}
```

## Feed discovery pipeline

```
aiograpi / requests          yt-dlp                  FFmpeg
┌──────────────┐    reel URL    ┌──────────────┐    video+audio    ┌──────────┐
│  clips/discover/  ─────────→  │  yt-dlp      ─────────────────→  │  player  │
│  (Reels tab API)  │            │  extract     │                    │          │
└──────────────┘            └──────────────┘                    └──────────┘
                              ↓ fallback
                         ┌──────────────┐
                         │  cache       │
                         │  reel_cache  │
                         │  .json       │
                         └──────────────┘
```

1. The bot calls `POST /api/v1/clips/discover/` on Instagram's internal API
   (authenticated via `instagram_cookies.txt` session cookies).
2. Each item with `media_type == 2` (video) is treated as a reel; its `code`
   becomes a `https://www.instagram.com/reel/<code>/` URL.
3. Pagination uses `paging_info.max_id` from the API response.
4. On 429 (rate-limit), retries with backoff (10s, 20s, 30s) up to 3 times.
5. If the API fails entirely, falls back through instaloader strategies, then
   yt-dlp flat-playlist, and finally a persistent local cache.

## Authentication

The bot authenticates to Instagram via Netscape-style cookies in
`instagram_cookies.txt` (looked up from the golive dir or parent dir).

The cookie file **must** contain a valid `sessionid` cookie for a logged-in
Instagram account.

```bash
# Inspect current session user
python3 -c "
import http.cookiejar
cj = http.cookiejar.MozillaCookieJar('instagram_cookies.txt')
cj.load()
for c in cj:
    if c.name == 'ds_user_id': print('User ID:', c.value)
"
```

## Configuration

| Env var                 | Default                      | Description                                                                                                                                                             |
| ----------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INSTAGRAM_REEL_SOURCE` | `https://www.instagram.com/` | Feed source URL for instaloader/yt-dlp fallback. Can be a profile (`https://www.instagram.com/username/`) or a hashtag (`https://www.instagram.com/explore/tags/tag/`). |

## Cache

Reel shortcodes are cached in `data/reel_cache.json` (up to 200 entries) so
playback continues across API rate-limits and restarts. The cache is shuffled
randomly each time it's used.

```json
{
  "shortcodes": ["DaHgRe2FZiV", "DaHb2oSk_dA", "..."],
  "last_updated": "2026-06-29T01:41:00"
}
```

## Troubleshooting

| Symptom                                         | Likely cause                                                                                                                                                              |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `[INSTA-API] rate-limit (429)`                  | Instagram is throttling the session. Wait a few minutes and retry. Avoid rapid repeated calls.                                                                            |
| `[INSTA-API] HTTP 4xx/5xx`                      | Session expired or network issue. Refresh cookies.                                                                                                                        |
| yt-dlp: `login required`                        | Instagram cookies are expired. Re-export from browser: go to instagram.com, open DevTools → Application → Cookies → export as Netscape format to `instagram_cookies.txt`. |
| `[INSTAGRAM] No se pudo extraer reel, saltando` | The reel is unavailable (deleted, private, or region-locked). Skipped automatically.                                                                                      |
| Feed returns 0 reels repeatedly                 | The authenticated account may have no reels or the session may be brand-new without algorithmic data. Try using URL mode with a specific reel link.                       |

## Files

| File                           | Role                                                                                                                      |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| `golive/bot.py`                | HTTP relay endpoint for `/instagram`                                                                                      |
| `golive/instagram_feed.py`     | `InstagramReelFeed` — queue-backed reel URL fetcher                                                                       |
| `golive/instagram_streamer.py` | `InstagramReelPlayer` + `InstagramGoLiveStream` — playback and GoLive lifecycle                                           |
| `golive/ytdlp.py`              | `_instagram_api_reel_feed_urls()` — Reels tab feed via `clips/discover/`; `_extract_instagram_sync()` — yt-dlp extraction |
| `data/reel_cache.json`         | Persistent shortcode cache (≤200 entries)                                                                                 |
