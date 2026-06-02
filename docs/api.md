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

## Error responses
Common errors:
- `401` – unauthorized (`X-API-Secret` mismatch)
- `400` – missing/invalid parameters
- `404` – guild or channel not found
- `409` – no active voice channel and no users to auto-pick
- `500` – Discord or playback failure

## Transcript forwarding
The userbot can POST transcripts to `BOT_API_BASE/transcript`, but this handler
is **not** implemented in `apiServer.py` and must be added separately.

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
- `preset`: integer 1, 2, or 3.
  - `1` — most sensitive: `che indio`, `que indio`, `eh indio` + command-verb patterns.
  - `2` — less sensitive (**default**): only `che indio` + command-verb patterns. Removes `que`/`eh` invocation pairs to reduce false positives.
  - `3` — placeholder/WIP: currently identical to preset 2.

The preset is **in-memory only** — it resets to the default (2) on userbot restart.

**Response**
```json
{ "preset": 2 }
```
- `400` if `preset` is missing, not an integer, or outside 1–3.
- `503` if `RELAY_SECRET` is not configured.
