# Operations (Runbook)

## Deployment scripts
- `deploy.sh`: Instala dependencias, crea venv, copia `.env`, y registra
  `discord-bot.service` para systemd.
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
- `userbot/vapls-userbot.service` (ajusta `User`, rutas y `.env`).

## Logging locations
- `play.log`: rotación de logs específicos de `/play`.
- `botOutput.log`: salida estándar (cuando se usa `runMonitored.sh` o `autoRestart.sh`).
- `monitorAutoKill.log`: reinicios de `autoRestart.sh`.
- Systemd: `journalctl -u discord-bot` y `journalctl -u vapls-userbot`.

## Troubleshooting
- **Bot no inicia**: verifica `TOKEN` (main bot) o `USER_TOKEN` (userbot).
- **No reproduce audio**: confirma `FFmpeg` instalado y `YT_DLP_PATH` válido.
- **No hay saludos/soundpad**: revisa `CUSTOM_AUDIO_PATH` y archivos existentes.
- **Gemini no responde**: revisa `GEMINI_API_KEY` y límites de cuota.
- **API devuelve 401**: el `X-API-Secret` no coincide con `API_SECRET`.
- **Userbot no transcribe**: revisa `MODEL_PATH_ES` y permisos del user token.
