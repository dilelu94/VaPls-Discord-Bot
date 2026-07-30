# Indio Instagram Local Scraper (DMs no-testers) 🚀

Este script de Python está diseñado para correr en tu **PC de casa (con tu IP residencial habitual)**. Su único objetivo es consultar la bandeja de entrada de Instagram de forma segura para responder a los usuarios que **no están agregados como testers** en la consola de desarrolladores de Meta (los cuales son invisibles para la API oficial de la nube).

*   **¿Por qué en tu PC y no en el servidor?** Para evitar el bloqueo geográfico (Geo-Velocity) de Instagram. Al correr con tu IP residencial de casa de siempre, Instagram no sospechará de accesos simultáneos conflictivos.
*   **¿Consume recursos?** Prácticamente cero (entre 30 MB y 50 MB de RAM, y 0% de CPU la mayor parte del tiempo).
*   **Frecuencia humana:** Hace consultas en intervalos aleatorios de entre 30 y 90 minutos, y se desactiva por completo en horarios de silencio (2 AM a 10 AM local).

---

## 🛠️ Requisitos e Instalación

1.  Asegúrate de tener Python 3.8+ instalado en tu máquina.
2.  Instala las dependencias necesarias abriendo una terminal en esta carpeta y ejecutando:
    ```bash
    pip install instagrapi requests
    ```

---

## ⚙️ Configuración del Script

Edita el archivo `scraper.py` y ajusta las siguientes variables en la sección de **CONFIGURACIÓN LOCAL**:

*   `INSTAGRAM_USERNAME`: Nombre de usuario de la cuenta de Instagram del Indio (`indio.goldstein` o la que corresponda).
*   `CLOUD_SERVER_URL`: La dirección IP y puerto públicos de tu servidor en Oracle Cloud (ej. `http://141.148.84.55:8080`).
*   `API_SECRET`: El mismo `API_SECRET` configurado en el archivo `.env` de tu bot en el servidor (para que tu bot autorice las peticiones del script local).
*   `ALLOWED_USERS`: Puedes dejarlo vacío `{}` para que responda a cualquiera que te escriba y esté pendiente, o llenarlo con una whitelist de nombres de usuario de Instagram en minúsculas (ej. `{"dilelu", "mati"}`) para limitar la interacción solo a ellos.

---

## 🚀 Ejecución Manual (Primera vez)

La primera vez que ejecutes el script, este te solicitará la contraseña de Instagram en la terminal para crear el archivo de sesión segura `instagram_cookies.json`. Las ejecuciones subsiguientes no requerirán contraseña mientras la sesión sea válida.

Ejecútalo con:
```bash
python scraper.py
```

---

## 🔄 Ejecución automática al iniciar la PC (Systemd User Service en Linux)

Como tu sistema operativo local es Linux, la forma más limpia y robusta de hacer que este script se ejecute automáticamente al encender tu PC y loguearte es mediante un **servicio de usuario de systemd**. Esto garantiza que corra en segundo plano, se reinicie si falla, y se ejecute sin requerir privilegios de superusuario (root).

### Paso 1: Crear el directorio de servicios de usuario (si no existe)
```bash
mkdir -p ~/.config/systemd/user/
```

### Paso 2: Crear el archivo de definición del servicio
Crea el archivo `~/.config/systemd/user/indio-instagram-scraper.service` con el siguiente contenido (asegúrate de reemplazar `/ruta/absoluta/a/...` por la ruta real donde guardaste este script en tu disco):

```ini
[Unit]
Description=Indio Instagram Local Scraper Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/ruta/absoluta/a/vapls-discord-bot/instagram_scraper
ExecStart=/usr/bin/python scraper.py
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

*Nota: Puedes verificar la ruta absoluta a tu ejecutable de Python corriendo `which python` o `which python3` en la terminal, y la ruta a tu carpeta con `pwd`.*

### Paso 3: Registrar y habilitar el servicio
Ejecuta los siguientes comandos en tu terminal de usuario:

```bash
# Recargar systemd para que detecte el nuevo servicio de usuario
systemctl --user daemon-reload

# Habilitar el servicio para que inicie automáticamente con el login
systemctl --user enable indio-instagram-scraper.service

# Iniciar el servicio inmediatamente
systemctl --user start indio-instagram-scraper.service

# Verificar el estado y que esté corriendo perfectamente
systemctl --user status indio-instagram-scraper.service
```

### Paso 4: Monitorear los logs en caliente
Puedes ver lo que está haciendo el script en tiempo real (consultas, detecciones de Reels, etc.) ejecutando:
```bash
journalctl --user -u indio-instagram-scraper.service -f
```
