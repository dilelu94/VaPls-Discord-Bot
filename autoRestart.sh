while true; do
    echo "[$(date)] Iniciando bot..." >> monitorAutoKill.log
    python3 -u bot.py >> botOutput.log 2>&1
    echo "[$(date)] Bot caído con código $?. Reiniciando en 5s..." >> monitorAutoKill.log
    sleep 5
done
