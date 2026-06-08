# Sistema MMR (Glicko-1)

## Overview

VaPls tiene un sistema de rating estilo Glicko-1 que asigna un puntaje MMR a cada usuario por servidor. Cada actividad (mensaje, voz, imagen, reacción, etc.) tiene un **peso** configurable que determina cuánto impacta en el rating.

El sistema se compone de dos partes que se comunican por HTTP:

- **Main bot** (`bot.py`, `_classify_and_log_message`): clasifica las actividades de Discord y las envía al userbot.
- **Userbot** (`userbot/activity_db.py`): recibe las actividades, calcula el delta Glicko-1 y persiste en SQLite.

## Admin page

URL pública: `http://141.148.84.55/admin`
Acceso local via SSH tunnel: `ssh -i ~/.ssh/vapls -L 8081:localhost:8080 ubuntu@141.148.84.55` → `http://localhost:8081/admin`
Credenciales: `dilelu` / `indiovapls`

La admin page tiene 4 pestañas:

| Pestaña      | Descripción                                                                              |
| ------------ | ---------------------------------------------------------------------------------------- |
| **Weights**  | Pesos de cada tipo de actividad. Se guardan en `config` de SQLite con prefijo `weight_`. |
| **Config**   | Variables internas del sistema Glicko (rating inicial, desviación, decay, etc.).         |
| **MMR**      | Ranking de todos los usuarios con su rating, desviación, actividades totales y premium.  |
| **Activity** | Historial de actividades recientes. Se puede filtrar por tipo.                           |

## Cómo se calcula el MMR

### 1. Peso (weight)

Cada actividad tiene un peso default en `userbot/activity_db.py:DEFAULT_WEIGHTS`:

```python
DEFAULT_WEIGHTS = {
    "voice_vad": 0.4,      # actividad de voz
    "camera": 0.8,         # cámara encendida
    "stream": 1.5,         # haciendo stream
    "watch_stream": 0.1,   # mirando stream
    "message": 0.3,        # mensaje de texto (sin link)
    "image": 0.8,          # imagen CON texto; sin texto → quality_score bajo
    "file": 0.6,           # archivo adjunto
    "link": 0.05,          # link (muy poco)
    "tiktok_link": -0.1,   # link de TikTok → DESCUENTA MMR
    "sticker": 0.01,       # sticker (casi nada)
    "thread_post": 1.5,    # post en thread
    "thread_create": 5.0,  # crear thread
    "forum_post": 2.0,     # post en foro
    "forum_create": 8.0,   # crear foro
    "reaction": 0.05,      # reacción (muy poco)
    "slash_command": 0.05, # comando slash (muy poco)
    "event_create": 6.0,   # crear evento
    "event_join": 1.0,     # unirse a evento
    "channel_create": 5.0, # crear canal
    "poll_create": 3.0,    # crear encuesta
    "poll_vote": 0.15,     # votar en encuesta
}
```

Los pesos se pueden cambiar en vivo desde la pestaña **Weights** de la admin page. Se persisten en la tabla `config` con clave `weight_<tipo>`.

### 2. Quality score

El `quality_score` (0.0 a 1.0) multiplica el impacto de la actividad:

```python
weight_factor = min(1.0, weight / 4.0)
actual = 0.5 + (q - 0.5) * weight_factor
```

- `q = 1.0` → máximo impacto (el peso se aplica al 100%)
- `q = 0.5` → impacto neutro (rating no cambia)
- `q = 0.0` → impacto mínimo

Casos donde el main bot reduce el quality_score:

| Situación                         | quality_score | Razón                                |
| --------------------------------- | ------------- | ------------------------------------ |
| Imagen **sin** texto              | `0.05`        | Casi no suma MMR                     |
| Mensaje de texto **fuera de voz** | `0.05`        | Poco MMR si no estás en canal de voz |
| Slash command **fuera de voz**    | `0.05`        | Poco MMR si no estás en canal de voz |

### 3. Fórmula Glicko-1

```
expected = 1 / (1 + 10^(-(rating - 1500) / 400))
new_r, new_rd = glicko_update(rating, deviation, actual, expected)
delta = new_r - rating
```

- `rating` inicial: 1500
- `deviation` inicial: 350
- `deviation` mínima: 30
- `actual` > `expected` → rating sube
- `actual` < `expected` → rating baja

### 4. Decay por inactividad

Si pasan más de 24 horas sin actividad:

- La desviación aumenta `decay_per_day` (default 10) por día
- El rating tiende a 1500 a razón de `decay_rating_per_day` (default 1) por día

## Dónde está cada cosa

### Main bot (`bot.py`)

| Location                                       | Qué hace                                                                      |
| ---------------------------------------------- | ----------------------------------------------------------------------------- |
| `_USERBOT_ID` (l.218)                          | ID del userbot a ignorar (519594605520486428)                                 |
| `_log_activity()` (l.151)                      | Envía actividad al userbot vía HTTP POST `/activity/log`                      |
| `_classify_and_log_message()` (l.221)          | Clasifica mensajes: imágenes, links, tiktok, texto, stickers, archivos, polls |
| `on_application_command()` (l.857)             | Trackea comandos slash como `slash_command`                                   |
| `on_message()` (l.626)                         | Entry point: filtra bots, deriva a `_classify_and_log_message`                |
| `on_voice_state_update()` (l.462)              | Trackea `voice_vad`, `camera`, `stream`, `watch_stream`                       |
| `on_raw_reaction_add()` (l.702)                | Trackea `reaction`                                                            |
| `on_guild_scheduled_event_subscribe()` (l.845) | Trackea `event_join`                                                          |

### Userbot (`userbot/activity_db.py`)

| Location                   | Qué hace                                                               |
| -------------------------- | ---------------------------------------------------------------------- |
| `DEFAULT_WEIGHTS` (l.18)   | Pesos default de cada tipo de actividad                                |
| `DEFAULT_CFG` (l.41)       | Config default del sistema Glicko                                      |
| `log_activity()` (l.272)   | Calcula y persiste el delta Glicko-1                                   |
| `_get_weight()` (l.198)    | Lee el peso de la DB o usa default                                     |
| `_detect_spam()` (l.213)   | Reduce quality si hay muchas actividades del mismo tipo en 10 segundos |
| `_glicko_update()` (l.256) | Implementación del algoritmo Glicko-1                                  |

### Userbot relay (`userbot/bot.py`)

| Location                         | Qué hace                                                          |
| -------------------------------- | ----------------------------------------------------------------- |
| `_relay_activity_log()` (l.3766) | Endpoint HTTP `/activity/log` que recibe actividades del main bot |
| `_relay_admin_data()`            | Endpoint que sirve datos a la admin page                          |
| `_relay_admin_weights()`         | Endpoint que persiste cambios de peso desde la admin page         |

### Config (`bot.py`)

| Variable de entorno  | Default            | Descripción                                            |
| -------------------- | ------------------ | ------------------------------------------------------ |
| `INDIO_RELAY_URL`    | —                  | URL del userbot para enviar actividades                |
| `INDIO_RELAY_SECRET` | —                  | Secreto compartido para autenticar requests al userbot |
| `ACTIVITY_DB_PATH`   | `data/activity.db` | Ruta a la base SQLite del MMR                          |

### Filtros especiales

- **Userbot ignorado**: el user ID `519594605520486428` se filtra en `_classify_and_log_message()` (main bot) y en `_relay_activity_log()` (userbot) para que sus actividades no cuenten.
- **Anti-spam**: `_detect_spam()` reduce quality si hay más de 3 actividades del mismo tipo en 10 segundos.
- **Premium**: si el usuario tiene flag premium, el quality_score se multiplica por `premium_multiplier` (default 0.85).

## Historial de cambios recientes

| Commit    | Cambio                                                                                                                                                             |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `7168189` | Ajuste de weights: link 0.05, tiktok_link -0.1, slash_command 0.05, sticker 0.01, reaction 0.05. Filtro de userbot. Calidad según voz. Tracking de slash commands. |
| `1e2d120` | Fix de escaping en onclick de la admin page (filterActivity).                                                                                                      |
| `8c8e94b` | Refactor: filterActivity como función global para evitar issues de escaping.                                                                                       |
| `9a8d915` | Fix: serializar user_id y guild_id como strings para evitar pérdida de precisión.                                                                                  |
