# Instagram Reels streaming (GoLive)

The `/instagram` relay endpoint streams Instagram Reels into a Discord voice
channel via GoLive (screen-share video + audio).

## Modes

| Mode     | Trigger                                            | Behavior                                                                                              |
| -------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| **URL**  | `{"url": "https://www.instagram.com/reel/XXXXX/"}` | Extracts that single reel via aiograpi, plays it once, then stops.                                    |
| **Feed** | `{}` (no `url`)                                    | Infinite scroll — discovers reel video URLs from the Reels tab feed via aiograpi, plays back-to-back. |

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
aiograpi                     FFmpeg
┌──────────────┐  video_url  ┌──────────┐
│  cl.reels()  │ ──────────→ │  player  │
│              │             │          │
│  (Reels tab  │             └──────────┘
│   feed API)  │                 ↑
└──────────────┘            ┌──────────────┐
                            │  cache       │
                            │  reel_cache  │
                            │  .json       │
                            └──────────────┘
```

1. `InstagramClient.get().discover(amount)` calls `cl.reels(amount)` on
   Instagram's private mobile API (authenticated via `instagram_cookies.txt`).
2. Each returned `Media` object with `media_type == 2` (video) provides a
   direct `video_url` — a muxed H.264+AAC stream, no DASH splitting needed.
3. Reel info (`shortcode`, `page_url`, `video_url`, `title`) is stored
   in an in-memory queue.
4. **Refill strategy**: 10 reels fetched at stream start, then +10 every 60s
   until the queue reaches 30 reels. The player reads from the queue
   synchronously in a daemon thread.
5. If the queue runs dry, falls back to a persistent local cache
   (`data/reel_cache.json` — shortcodes only, re-extracted via yt-dlp).

## Authentication

The bot authenticates to Instagram via `aiograpi`, which uses Instagram's
private mobile API (not the rate-limited web API).

**Session persistence:**

On first run, `InstagramClient` reads the `sessionid` cookie from
`instagram_cookies.txt` (Netscape format) and calls
`cl.login_by_sessionid(sid)`. The resulting device fingerprint + cookies
are saved to `instagram_session.json` and reused on subsequent runs via
`cl.load_settings()`.

```bash
# Inspect current session file
python3 -c "
import json
s = json.load(open('golive/instagram_session.json'))
print('User ID:', s.get('ds_user_id'))
print('Cookies:', len(s.get('cookies', {})))
"
```

The cookie file **must** contain a valid `sessionid` cookie for a logged-in
Instagram account. Export from browser:

1. Go to instagram.com and log in
2. Open DevTools → Application → Cookies → `instagram.com`
3. Copy the `sessionid` value, then create/update `instagram_cookies.txt`:
   ```
   # Netscape HTTP Cookie File
   .instagram.com	TRUE	/	FALSE	0	sessionid	<your_sessionid_here>
   ```

## Authentication troubleshooting

| Symptom                      | Likely cause                                                                        |
| ---------------------------- | ----------------------------------------------------------------------------------- |
| `sessionid login failed`     | The browser/web `sessionid` was rejected by Instagram's mobile API. See note below. |
| `login_required` from server | Session expired. Re-export `sessionid` from browser or use username/password login. |

If `login_by_sessionid()` consistently fails, Instagram may be rejecting
browser/web `sessionid` tokens for the private mobile API. The aiograpi
docs recommend a one-time password login, then reuse the saved session:

```python
from aiograpi import Client

cl = Client()
await cl.login("username", "password")
cl.dump_settings("instagram_session.json")
```

Set `IG_USERNAME` / `IG_PASSWORD` env vars and trigger a URL-mode request
to perform the initial login (or run the snippet above manually).

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

## Configuration

| Env var         | Default | Description                                                                                      |
| --------------- | ------- | ------------------------------------------------------------------------------------------------ |
| _(none needed)_ | —       | Auth is handled by `instagram_cookies.txt` + `instagram_session.json`. No env vars are required. |

## Troubleshooting

| Symptom                                         | Likely cause                                                                                              |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `[INSTA-CLIENT] reels() failed`                 | aiograpi API call failed — session may be expired or rate-limited. Refresh cookies.                       |
| `[INSTA-CLIENT] Not ready, can't discover`      | Client couldn't log in. Check cookies file exists with valid `sessionid`.                                 |
| `aiograpi not installed`                        | Run `pip install aiograpi` on the server.                                                                 |
| yt-dlp: `login required` (cache fallback)       | Instagram cookies are expired for yt-dlp. Only affects cache fallback — feed mode uses aiograpi directly. |
| `[INSTAGRAM] No se pudo extraer reel, saltando` | The reel is unavailable (deleted, private, or region-locked). Skipped automatically.                      |
| Feed returns 0 reels repeatedly                 | The authenticated account has no reels or the session is new. Try URL mode with a specific reel link.     |

## Files

| File                            | Role                                                                                                         |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `golive/instagram_client.py`    | `InstagramClient` — async wrapper around `aiograpi.Client`, session management, `reels()` and `media_info()` |
| `golive/instagram_feed.py`      | `InstagramReelFeed` — queue-backed reel URL fetcher using `InstagramClient.discover()`                       |
| `golive/instagram_streamer.py`  | `InstagramReelPlayer` + `InstagramGoLiveStream` — playback and GoLive lifecycle                              |
| `golive/bot.py`                 | HTTP relay endpoint for `/instagram`                                                                         |
| `golive/ytdlp.py`               | Contains `_extract_instagram_sync()` — only used as cache fallback when aiograpi `video_url` is unavailable  |
| `golive/instagram_session.json` | Persisted aiograpi session (device fingerprint + cookies) — auto-generated on first successful login         |
| `data/reel_cache.json`          | Persistent shortcode cache (≤200 entries)                                                                    |
