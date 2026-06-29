# HTTP API

The API is served by `apiServer.py` on `http://{API_HOST}:{API_PORT}`.
All requests must include the header:

```
X-API-Secret: <API_SECRET>
```

If `API_SECRET` is empty, the API returns `503` for all endpoints.

## Endpoints

### GET `/status`

Returns readiness and voice client status.

**Response**

```json
{
  "ready": true,
  "guilds": 2,
  "voice_clients": [
    {
      "guild_id": 123,
      "channel_id": 456,
      "channel_name": "General",
      "playing": true
    }
  ]
}
```

### GET `/members?guild_id=...&voice_only=true|false`

Lists voice channels and members for a guild.

**Query params**

- `guild_id` (required)
- `voice_only` (optional, default `true`)

**Response**

```json
{
  "voice_channels": [
    {
      "id": 1,
      "name": "General",
      "members": [{ "id": 2, "display_name": "User", "is_bot": false }]
    }
  ],
  "guild_members": []
}
```

### GET `/user/{user_id}?guild_id=...`

Returns a single guild member’s status and voice state.

### POST `/message`

Posts a message to a text channel.

**Body (JSON)**

```json
{
  "guild_id": 123,
  "channel_id": 456,
  "content": "hola",
  "sender_label": "TG"
}
```

**Response**

```json
{ "message_id": 789 }
```

### POST `/play-audio`

Plays an uploaded audio file in a voice channel.

**Body (multipart/form-data)**

- `guild_id` (required)
- `channel_id` (optional)
- `file` (required)
- `reply_callback_url` (optional) — when set, after playback finishes the
  bot asks the userbot to capture the voice channel's reply (up to
  `reply_duration` seconds, default `USERBOT_RECORD_DEFAULT_DURATION`),
  encode it to OGG/Opus, and POST it to this URL as multipart with fields:
  `file` (the audio), `metadata` (the verbatim value you passed in
  `reply_metadata`), `guild_id`, `channel_id`, `duration_seconds`.
  Nothing is delivered when no one spoke in the channel during the window.
- `reply_callback_secret` (optional) — sent as `X-API-Secret` on the
  callback request.
- `reply_metadata` (optional) — opaque payload (JSON string recommended)
  echoed back so the Telegram bridge can route the audio to the originating
  chat/message.
- `reply_duration` (optional) — recording length in seconds; clamped to
  `[1, RECORD_MAX_SECONDS]` on the userbot side.

**Response**

```json
{
  "played": true,
  "channel_id": 456,
  "channel_name": "General",
  "will_record_reply": true
}
```

`will_record_reply` is `true` only when `reply_callback_url` was supplied
and the bot is configured (`USERBOT_RECORD_URL`) to forward to the userbot.

### GET `/queue?guild_id=...`

Returns the current playback queue.

**Response**

```json
{
  "current": { "id": "abc", "title": "Song" },
  "queue": [],
  "history_count": 2,
  "is_paused": false,
  "is_playing": true
}
```

## File transfer endpoints (`/upload`, `/dl`)

These endpoints are **public** (no `X-API-Secret` required). The UUID token is the
authentication mechanism.

### `GET /upload/{token}`

Serves the HTML upload page. Shows an upload form, a listing of all active files,
the permanent upload history, and disk usage.

### `POST /upload/{token}/init`

Initialise a chunked upload session.

**Body (JSON)**

```json
{ "filename": "video.mp4", "size": 2147483648 }
```

- `filename`: the original file name.
- `size`: total file size in bytes (must be ≤ `TRANSFER_MAX_SIZE`).
- Returns `400` if the session is expired, disk is full, or size exceeds the limit.

### `POST /upload/{token}/chunk/{idx}`

Upload a single chunk. The body is raw binary. `idx` is the zero-based chunk index.

Chunks are written at the correct offset in the target file as they arrive, so
the upload is **resumable**: if interrupted, the client can call `/status` to
learn which chunks are already on disk and resume from the first missing one.

- Returns `400` if the session is expired or the upload is already complete.

### `GET /upload/{token}/status`

Returns the current session state.

**Response**

```json
{
  "valid": true,
  "expired": false,
  "completed": false,
  "received": [0, 1, 2],
  "total_chunks": 200,
  "filename": "video.mp4",
  "size": 2147483648,
  "ttl_remaining": 240
}
```

- `ttl_remaining`: seconds before the session token expires (only meaningful
  when the upload is not yet complete). When the file is ready, returns 86400.

### `POST /upload/{token}/complete`

Finalise the upload. Verifies that the assembled file size matches the declared
`size` from `/init`. Once confirmed, the file is marked `ready` and the bot
automatically posts the download link to the Discord channel.

- Returns `400` if the size does not match.

### `DELETE /upload/{token}`

Delete the session and its file from disk. Any valid token holder can delete any
file (community cleanup model).

**Response**

```json
{ "ok": true }
```

### `GET /upload/{token}/files`

Returns JSON with all currently active files (not expired) and disk usage.

**Response**

```json
{
  "files": [
    {
      "token": "abc123",
      "filename": "video.mp4",
      "size": 2147483648,
      "author_name": "Mati",
      "remaining_secs": 64800,
      "url": "http://141.148.84.55/dl/abc123/video.mp4"
    }
  ],
  "disk_free": 30000000000,
  "disk_total": 45000000000
}
```

### `GET /upload/{token}/history`

Returns the permanent upload history (last 200 entries).

**Response**

```json
{
  "history": [
    {
      "author_name": "Mati",
      "filename": "video.mp4",
      "size": 2147483648,
      "uploaded_at": 1750000000,
      "token": "abc123"
    }
  ]
}
```

### `GET /dl/{token}/{filename}`

Download a completed file. Serves the file with `Content-Disposition: attachment`.

### `GET /dl/{token}`

Redirects to `/dl/{token}/{filename}` when there is exactly one file.

## Error responses

Common errors:

- `401` – unauthorized (`X-API-Secret` mismatch)
- `400` – missing/invalid parameters
- `404` – guild or channel not found
- `409` – no active voice channel and no users to auto-pick
- `500` – Discord or playback failure

## Telegram memory pipeline

Endpoints called by the Telegram bridge (`vapls-telegram-bot`) to feed messages
into the Indio's long-term memory.

### POST `/telegram-message`

Injects a text message into Indio's per-guild conversation history (no reply is
generated — Indio only "listens"). Periodic compression will distill it into
long-term notes (traits, anecdotes, inside jokes).

**Body (JSON)**

```json
{
  "guild_id": 451575911704428554,
  "speaker": "Mati",
  "text": "hola que hace",
  "ts": 1712345678.0
}
```

- `guild_id` (required) — the Discord guild whose Indio memory bucket to use.
- `speaker` (required) — the display name (Telegram name, Discord name, or any
  label the Indio will recognize).
- `text` (required) — the message content.
- `ts` (optional, default `time.time()`) — Unix timestamp of the original
  message. Keeps the temporal ordering correct during compression.

**Response**

```json
{ "ok": true }
```

Processing is fire-and-forget (`asyncio.create_task`). The endpoint returns
immediately.

### POST `/telegram-image`

Receives an image from the Telegram bridge, sends it to Gemini for a short
textual description, then injects the description into Indio's history as
`"{speaker}: [imagen: {description}]"`.

**Body (multipart/form-data)**

- `guild_id` (required) — Discord guild ID.
- `speaker` (required) — display name.
- `ts` (optional) — Unix timestamp.
- `file` (required) — image binary (JPEG, PNG, GIF, or WebP).

**Response**

```json
{
  "ok": true,
  "description": "una gata naranja durmiendo en un teclado"
}
```

The description is truncated to 200 characters in the response for display
purposes; the full description is stored in Indio's memory.

## Transcript forwarding

---

## Userbot relay API

These endpoints are served by the **userbot** (`apiServer` on `127.0.0.1:8081`).
All requests must include `X-API-Secret: <RELAY_SECRET>`.

### POST `/sensibilidad`

Switch the VOSK wake-word sensitivity preset at runtime.

**Body**

```json
{ "preset": 2 }
```

- `preset`: integer 1, 2, 3, or 4.
  - `1` — most sensitive: `che indio`, `que indio`, `eh indio` + command-verb patterns.
  - `2` — less sensitive: only `che indio` + command-verb patterns. Removes `que`/`eh` invocation pairs to reduce false positives.
  - `3` — enlarged grammar pool: re-enables `che indio`, `que indio`, `eh indio` + command-verb patterns (same as preset 1), but adds a large decoy token pool (`_PRESET_3_FILLER`) so VOSK has many buckets for ambient speech instead of collapsing noise into wake-word phrases. Pool is hand-editable in `userbot/bot.py`.
  - `4` — (**default**) same VOSK gating as preset 2 (`che indio` + command-verb patterns, small grammar pool), but VOSK runs single-best with `SetWords(True)`. Second post-VOSK layer: the exact "indio" word audio is sliced out of the segment buffer using VOSK's per-word timestamps and a short Whisper pass (`_run_whisper_wake`) runs on just that clip. If Whisper cannot confirm "indio", the event is discarded (no command transcription, no dispatch). This isolates the wake word regardless of where VOSK fired (the old fixed-prebuffer slice missed it when VOSK fired late on the command verb).

The preset is **in-memory only** — it resets to the default (4) on userbot restart.

**Response**

```json
{ "preset": 2 }
```

- `400` if `preset` is missing, not an integer, or outside 1–4.
- `503` if `RELAY_SECRET` is not configured.

---

### POST `/instagram` (GoLive relay)

Relay endpoint to start an Instagram Reels GoLive stream. Served by
`golive/bot.py` (not the main API server).

**Headers**

```
X-API-Secret: <RELAY_SECRET>
```

**Request body**

```json
{
  "guild_id": 451575911704428554,
  "channel_id": 1089025651786404030,
  "url": "https://www.instagram.com/reel/XXXXX/"
}
```

| Field        | Required | Description                                          |
| ------------ | -------- | ---------------------------------------------------- |
| `guild_id`   | yes      | Discord guild ID                                     |
| `channel_id` | yes      | Discord voice channel ID                             |
| `url`        | no       | Single reel URL. Omit for infinite-scroll feed mode. |

**Response (success, `200`)**

```json
{
  "started": true,
  "guild_id": 451575911704428554,
  "channel_name": "General",
  "video_ssrc": 21810
}
```

**Errors**

- `503` if `RELAY_SECRET` is empty or client not ready.
- `401` if `X-API-Secret` header is wrong.
- `400` if body is malformed, missing fields, or channel is not a voice channel.
- `404` if guild or channel not found.
- `403` if guild is not in the allowed list.
- `500` if voice join fails, yt-dlp extraction fails, or stream start fails.

**See also:** [Instagram streaming docs](instagram.md).
