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
    sudo apt-get install -y python3-pip python3-venv ffmpeg git wget unzip
elif [[ "$OS_ID" == "ol" || "$OS_ID" == "rhel" || "$OS_ID" == "centos" || "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" ]]; then
    echo "Instalando dependencias mediante dnf..."
    sudo dnf install -y python3 python3-pip git wget tar xz unzip
    
    # Instalar FFmpeg mediante binario estático ya que no está en los repos oficiales por defecto
    if ! command -v ffmpeg &> /dev/null; then
        echo "Instalando FFmpeg (binario estático)..."
        wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
        tar -xf ffmpeg-release-amd64-static.tar.xz
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

# Instalar/actualizar yt-dlp de forma global
echo "Instalando yt-dlp..."
if sudo wget -q https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp; then
    sudo chmod a+rx /usr/local/bin/yt-dlp
    echo "yt-dlp instalado correctamente en /usr/local/bin/yt-dlp"
else
    echo "⚠️ Error al descargar yt-dlp desde GitHub. Se intentará usar pip más adelante."
fi

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

# 4. Crear el servicio de systemd para control 24/7
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

# Recargar systemd para reconocer el nuevo servicio
sudo systemctl daemon-reload
sudo systemctl enable discord-bot

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
