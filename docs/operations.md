# Operations (Runbook)

## Server

- **Producción**: Oracle Cloud Ampere A1 — 4 OCPU (Neoverse N1 aarch64) / 24 GB RAM.
- **Stack**: Ubuntu 22.04+ aarch64. `faster-whisper` (CTranslate2 con wheels aarch64), `py-cord`, `discord.py-self`, `ffmpeg`.
- **⚠️ Python 3.10 (constraint de runtime)**: el server corre **Python 3.10.12**
  (el `python3` que trae Ubuntu 22.04), en ambos venvs (`venv/`, `userbot/venv/`).
  **Esa es la única versión soportada en producción** y la única sobre la que
  gatea la CI. Si bumpeás el target de Python, actualizá la matriz en
  `.github/workflows/ci.yml` y este doc en el mismo cambio.
- **Razón del upgrade desde E2.1.Micro (1 GB)**: faster-whisper `base` saturaba la CPU (~27s para 1.4s de audio); el modelo `small` ahora corre real-time con concurrencia 5 y deja headroom para `/play` simultáneo.

## CI/CD pipeline

El deploy normal es **automático**: hacer push a `master` dispara
`.github/workflows/ci.yml`.

```
push a master ─► job test (Python 3.10, = prod) ─► job deploy (SSH al server)
                     │ falla ⇒ no deploya        │ corre scripts/deploy.sh
```

- **`deploy` corre solo** tras pasar el job de tests (Python 3.10), solo en
  pushes a `master`, y **se saltea sin fallar** si el secret `SSH_HOST` no está
  seteado.
- El runner de GitHub se conecta por SSH (secrets `SSH_HOST` / `SSH_USER` /
  `SSH_KEY`, opcional `SSH_PORT`) y **pipea `scripts/deploy.sh` por stdin**
  (`bash -s`), así corre la versión del repo aunque el checkout del server esté
  viejo o roto.
- **`scripts/deploy.sh`** (idempotente, corre en el server): `git fetch` +
  `git reset --hard origin/master` (el server es un _pure deploy target_ — no
  editar archivos a mano ahí), reinstala deps solo si cambiaron
  `requirements.txt` / `userbot/requirements.txt`, reinicia ambos servicios y
  **verifica que queden `active`** (si no, sale con error y el deploy figura rojo).
- **Las git-deps están pinneadas a propósito** (`py-cord`, `discord.py-self`,
  `discord-ext-voice-recv`). Un dep de git sin pin tumbó el userbot una vez al
  reinstalar un master roto. Bumpeá los SHAs deliberadamente + testeá, nunca auto.

Deploy manual (fallback, p. ej. para tocar solo un archivo sin pasar por CI):

```bash
rsync -avz -e "ssh -i <key>" <archivos> ubuntu@<host>:/home/ubuntu/vapls-discord-bot/
ssh -i <key> ubuntu@<host> 'sudo systemctl restart discord-bot indio-userbot'
```

## Provisioning scripts (primer arranque / clon nuevo)

- `deploy.sh`: Instala dependencias, crea venv, copia `.env`, y registra
  `discord-bot.service` para systemd. (Provisión inicial — el deploy continuo
  usa `scripts/deploy.sh`, ver arriba.)
- `run.sh`: Ejecuta `python3 bot.py` en primer plano.
- `runMonitored.sh`: Redirige logs a `botOutput.log`.
- `autoRestart.sh`: Bucle de reinicio con logs en `botOutput.log` y
  `monitorAutoKill.log`.

## Systemd

Main bot service (from `deploy.sh`):

```
sudo systemctl start discord-bot
sudo systemctl status discord-bot
journalctl -u discord-bot -f
```

Userbot service example:

- `userbot/indio-userbot.service` (ajusta `User`, rutas y `.env`).

## Logging locations

- `play.log`: rotación de logs específicos de `/play`.
- `botOutput.log`: salida estándar (cuando se usa `runMonitored.sh` o `autoRestart.sh`).
- `monitorAutoKill.log`: reinicios de `autoRestart.sh`.
- Systemd: `journalctl -u discord-bot` y `journalctl -u indio-userbot`.

## Troubleshooting

- **Bot no inicia**: verifica `TOKEN` (main bot) o `USER_TOKEN` (userbot).
- **No reproduce audio**: confirma `FFmpeg` instalado y `YT_DLP_PATH` válido.
- **No hay saludos/soundpad**: revisa `CUSTOM_AUDIO_PATH` y archivos existentes.
- **Gemini no responde**: revisa `GEMINI_API_KEY` y límites de cuota.
- **API devuelve 401**: el `X-API-Secret` no coincide con `API_SECRET`.
- **Userbot no transcribe**: revisa `MODEL_PATH_ES` y permisos del user token.
- **Deploy falla en GitHub Actions**: si el job `deploy` queda rojo tras un push
  a `master`, casi siempre es que `scripts/deploy.sh` reinició un servicio que no
  quedó `active` (mirá `journalctl -u <svc>` en el server). Una git-dep
  reinstalada a un master roto es el sospechoso clásico — verificá que sigan
  pinneadas.
