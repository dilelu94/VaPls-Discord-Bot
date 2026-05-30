#!/usr/bin/env bash
# Script para automatizar la configuración y despliegue del bot de Discord en Ubuntu u Oracle Linux
# Debe ejecutarse en el servidor, dentro de la carpeta del proyecto.

set -e

echo "=== Iniciando configuración del servidor ==="

# Detectar el sistema operativo
OS_ID=""
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID=$ID
fi

echo "Sistema operativo detectado: $OS_ID"

# 1. Instalar dependencias del sistema según el SO
if [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
    echo "Instalando dependencias mediante apt..."
    sudo apt-get update
    sudo apt-get install -y python3-pip python3-venv ffmpeg libopus0 libsodium23 git wget unzip
elif [[ "$OS_ID" == "ol" || "$OS_ID" == "rhel" || "$OS_ID" == "centos" || "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" ]]; then
    echo "Instalando dependencias mediante dnf..."
    sudo dnf install -y python3 python3-pip git wget tar xz unzip

    # Instalar FFmpeg mediante binario estático ya que no está en los repos oficiales por defecto
    if ! command -v ffmpeg &> /dev/null; then
        case "$(uname -m)" in
            x86_64)  FFMPEG_ARCH=amd64 ;;
            aarch64) FFMPEG_ARCH=arm64 ;;
            *) echo "Arquitectura no soportada para FFmpeg estático: $(uname -m)"; exit 1 ;;
        esac
        echo "Instalando FFmpeg (binario estático ${FFMPEG_ARCH})..."
        wget -q "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz"
        tar -xf "ffmpeg-release-${FFMPEG_ARCH}-static.tar.xz"
        sudo mv ffmpeg-*-static/ffmpeg /usr/local/bin/
        sudo mv ffmpeg-*-static/ffprobe /usr/local/bin/
        rm -rf ffmpeg-*-static*
        echo "FFmpeg instalado en /usr/local/bin/ffmpeg"
    else
        echo "FFmpeg ya está instalado."
    fi
else
    echo "⚠️ Sistema operativo no soportado automáticamente. Intentando instalar paquetes básicos..."
    sudo pacman -S python python-pip ffmpeg git || sudo yum install python3 python3-pip ffmpeg git || true
fi

# Instalar yt-dlp nightly + plugin bgutil POT en ~/.local/bin (user-scope).
# El plugin necesita estar en el MISMO Python env que yt-dlp para que lo detecte.
# Apuntá YT_DLP_PATH=$HOME/.local/bin/yt-dlp en .env.
echo "Instalando yt-dlp nightly + plugin bgutil-ytdlp-pot-provider..."
pip install --user --upgrade --pre 'yt-dlp[default]' bgutil-ytdlp-pot-provider
echo "yt-dlp instalado en $HOME/.local/bin/yt-dlp"

# Instalar deno (requerido por yt-dlp para extraer videos de YouTube)
if ! command -v deno &> /dev/null; then
    echo "Instalando deno (JS runtime requerido por yt-dlp)..."
    curl -fsSL https://deno.land/install.sh | sh -s -- -y
    if [ -x "$HOME/.deno/bin/deno" ]; then
        sudo ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
        echo "deno instalado y enlazado en /usr/local/bin/deno"
    else
        echo "⚠️ La instalación de deno falló."
    fi
else
    echo "deno ya está instalado."
fi

# Instalar Node.js (necesario para el bgutil-pot provider server)
if ! command -v node &> /dev/null || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 18 ]; then
    echo "Instalando Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    echo "Node.js $(node -v) ya está instalado."
fi

# Instalar bgutil-pot-provider (server Node.js que genera PO Tokens para evitar
# el bot-check de YouTube). Corre en localhost:4416 y lo consume yt-dlp vía el
# plugin instalado arriba. Configurá YT_DLP_POT_BASE_URL=http://127.0.0.1:4416 en .env.
POT_DIR="$HOME/bgutil-pot-provider"
if [ ! -d "$POT_DIR" ]; then
    echo "Clonando bgutil-pot-provider en $POT_DIR..."
    git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_DIR"
fi
echo "Building bgutil-pot-provider server..."
pushd "$POT_DIR/server" > /dev/null
npm install --no-audit --no-fund
npx --yes tsc
popd > /dev/null

# Systemd unit para el provider
echo "Configurando bgutil-pot.service..."
cat <<EOF | sudo tee /etc/systemd/system/bgutil-pot.service > /dev/null
[Unit]
Description=bgutil-pot YouTube POT provider
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${POT_DIR}/server
ExecStart=/usr/bin/node ${POT_DIR}/server/build/main.js
Restart=on-failure
RestartSec=5
Environment=PORT=4416

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now bgutil-pot.service
echo "bgutil-pot.service activo en puerto 4416"

# 2. Configurar el entorno virtual de Python
echo "Creando entorno virtual de Python..."
python3 -m venv venv

echo "Instalando dependencias de Python..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Configurar archivo .env si no existe
if [ ! -f .env ]; then
    echo "Creando archivo .env desde .env.example..."
    cp .env.example .env
    echo "⚠️  ATENCIÓN: Se ha creado el archivo .env. Debes editarlo con tus credenciales usando: nano .env"
else
    echo "El archivo .env ya existe."
fi

# 4. Crear directorio de estado persistente del main bot (INDIO_MEMORY_PATH)
mkdir -p data

# 5. Configurar entorno virtual + .env + servicio del userbot
if [ -d userbot ]; then
    echo "Configurando entorno virtual del userbot..."
    pushd userbot > /dev/null
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    # discord-ext-voice-recv arrastra discord.py como dep transitiva y
    # colisiona con discord.py-self en el namespace `discord` (rompe con
    # "Client.__init__() missing 'intents'"). Forzar a discord.py-self
    # a ganar el namespace.
    pip uninstall -y discord.py 2>/dev/null || true
    pip install --force-reinstall --no-deps "discord.py-self[voice] @ git+https://github.com/dolfies/discord.py-self"
    deactivate
    popd > /dev/null

    if [ ! -f userbot/.env ]; then
        echo "Creando userbot/.env desde userbot/.env.example..."
        cp userbot/.env.example userbot/.env
        echo "⚠️  ATENCIÓN: editá userbot/.env con USER_TOKEN y demás credenciales."
    else
        echo "El archivo userbot/.env ya existe."
    fi
else
    echo "⚠️  Directorio userbot/ no encontrado, salteando provisión del userbot."
fi

# 6. Crear el servicio de systemd para control 24/7
echo "Configurando el servicio de systemd (discord-bot.service)..."
CURRENT_DIR=$(pwd)
CURRENT_USER=$USER

cat <<EOF | sudo tee /etc/systemd/system/discord-bot.service > /dev/null
[Unit]
Description=Discord Bot for VaPls
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${CURRENT_DIR}
ExecStart=${CURRENT_DIR}/venv/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 7. Registrar el servicio del userbot (unit versionado en el repo)
if [ -f userbot/vapls-userbot.service ]; then
    echo "Instalando vapls-userbot.service..."
    sudo cp userbot/vapls-userbot.service /etc/systemd/system/vapls-userbot.service
fi

# Recargar systemd para reconocer los servicios nuevos
sudo systemctl daemon-reload
sudo systemctl enable discord-bot
if [ -f /etc/systemd/system/vapls-userbot.service ]; then
    sudo systemctl enable vapls-userbot
fi

echo "=== Configuración completada con éxito ==="
echo ""
echo "Pasos siguientes sugeridos:"
echo " 1. Edita el archivo de configuración con tus tokens:"
echo "    nano .env"
echo " 2. Inicia el bot con el siguiente comando:"
echo "    sudo systemctl start discord-bot"
echo " 3. Verifica que el bot esté funcionando correctamente:"
echo "    sudo systemctl status discord-bot"
echo " 4. Para ver los logs en tiempo real:"
echo "    journalctl -u discord-bot -f"
echo ""
