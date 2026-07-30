#!/usr/bin/env python3
import os
import time
import json
import random
import logging
import base64
import requests
from instagrapi import Client
from instagrapi.exceptions import ClientError

# Configurar logging profesional
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("instagram_scraper")

# --- CONFIGURACIÓN LOCAL ---
INSTAGRAM_USERNAME = "indio.goldstein"  # Tu usuario de Instagram del Indio (cuenta secundaria)
COOKIES_PATH = f"instagram_cookies_{INSTAGRAM_USERNAME}.json"  # Ruta para persistir la sesión dinámica
PROCESSED_COMMENTS_PATH = "processed_comments.json"  # Registro de notificaciones de comentarios respondidas

# Dirección de tu servidor en Oracle Cloud (cambiar si usas otra IP/puerto)
CLOUD_SERVER_URL = "http://141.148.84.55:8080"
API_SECRET = "tu_secreto_aqui"  # Debe coincidir con el API_SECRET de tu bot (.env)

# Rango de espera aleatorio entre consultas de bandeja (en segundos)
MIN_DELAY_SECS = 1800   # 30 minutos
MAX_DELAY_SECS = 5400   # 1 hora y media

# Horario de silencio/sueño (en base a la hora local de tu PC)
SILENCE_START_HOUR = 2  # 2 AM
SILENCE_END_HOUR = 10   # 10 AM

# Lista de usuarios de Instagram autorizados a interactuar con el Indio.
# Si la dejas vacía, responderá a cualquier conversación pendiente.
ALLOWED_USERS = {
    # "dilelu", "mati", "fidel"
}

def check_silence_time():
    import datetime
    now = datetime.datetime.now()
    hour = now.hour
    if SILENCE_START_HOUR < SILENCE_END_HOUR:
        return SILENCE_START_HOUR <= hour < SILENCE_END_HOUR
    else: # Horario cruza la medianoche (ej: de 22:00 a 06:00)
        return hour >= SILENCE_START_HOUR or hour < SILENCE_END_HOUR

def load_processed_comments():
    if os.path.exists(PROCESSED_COMMENTS_PATH):
        try:
            with open(PROCESSED_COMMENTS_PATH, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_processed_comment(comment_pk):
    processed = load_processed_comments()
    processed.add(comment_pk)
    try:
        with open(PROCESSED_COMMENTS_PATH, "w") as f:
            json.dump(list(processed), f)
    except Exception as e:
        logger.error(f"Error al guardar comentario procesado: {e}")

def fetch_gemini_reply(username, text, reel_caption, image_b64=None):
    url = f"{CLOUD_SERVER_URL}/instagram/generate-reply"
    headers = {"X-API-Secret": API_SECRET}
    payload = {
        "username": username,
        "text": text,
        "reel_caption": reel_caption,
        "image_b64": image_b64 or ""
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=40)
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Error del servidor cloud ({response.status_code}): {response.text}")
    except Exception as e:
        logger.error(f"Error de conexión con el servidor cloud: {e}")
    return None

def download_image_b64(cl, image_url):
    """Descarga una imagen de los CDNs de Instagram y la convierte a base64."""
    if not image_url:
        return None
    try:
        resp = cl.private.get(image_url)
        if resp.status_code == 200:
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logger.error(f"Error al descargar imagen: {e}")
    return None

def process_inbox(cl):
    """Revisa y responde mensajes directos (DMs) de texto, Reels e Historias."""
    logger.info("Revisando la bandeja de entrada de Instagram (DMs)...")
    try:
        # Usamos amount=10 en lugar de limit=10 (firma correcta de instagrapi)
        threads = cl.direct_threads(amount=10)
        
        for thread in threads:
            if not thread.messages:
                continue
            
            last_msg = thread.messages[0]
            
            # Ignorar si el último mensaje lo enviamos nosotros (ya respondido)
            if last_msg.user_id == cl.user_id:
                continue
                
            sender_username = thread.thread_title.lower()
            
            # Si no está en la lista de usuarios autorizados, ignorar
            if ALLOWED_USERS and sender_username not in ALLOWED_USERS:
                continue
            
            logger.info(f"DM pendiente detectado de @{sender_username}")
            
            pregunta = ""
            reel_caption = ""
            image_b64 = None
            is_story = False
            is_reel = False
            
            # 1. Mención en Historia (Story Share)
            if last_msg.story_share:
                is_story = True
                story = last_msg.story_share
                image_url = None
                if story.media:
                    if hasattr(story.media, "thumbnail_url") and story.media.thumbnail_url:
                        image_url = story.media.thumbnail_url
                    elif hasattr(story.media, "image_versions2") and story.media.image_versions2:
                        candidates = story.media.image_versions2.get("candidates", [])
                        if candidates:
                            image_url = candidates[0].get("url")
                
                if image_url:
                    logger.info(f"Mención en historia detectada de @{sender_username}. Descargando imagen...")
                    image_b64 = download_image_b64(cl, image_url)
                else:
                    logger.info(f"Mención en historia de @{sender_username} sin imagen descargable.")
                pregunta = story.title or ""

            # 2. Reel de Instagram compartido
            elif last_msg.clip:
                is_reel = True
                reel_caption = last_msg.clip.caption_text or ""
                logger.info(f"El usuario compartió un Reel. Caption: '{reel_caption}'")

            # 3. Mensaje de texto simple
            elif last_msg.text:
                pregunta = last_msg.text
                logger.info(f"El usuario envió un texto: '{pregunta}'")
            else:
                logger.info("Tipo de mensaje no soportado (imagen, audio, etc.). Ignorando.")
                continue
            
            # Consultar al servidor Cloud por la respuesta
            reply_data = fetch_gemini_reply(sender_username, pregunta, reel_caption, image_b64)
            if not reply_data:
                continue
            
            reply = reply_data.get("reply")
            react = reply_data.get("react")
            
            # Ejecutar la respuesta en Instagram
            if react == "heart":
                logger.info(f"Reaccionando con ❤️ en chat de @{sender_username}")
                try:
                    cl.direct_message_react(thread.id, last_msg.id, "❤️")
                except Exception as e:
                    logger.error(f"Error al reaccionar al mensaje: {e}")
            elif reply:
                logger.info(f"Enviando respuesta a @{sender_username}: '{reply}'")
                try:
                    cl.direct_send(reply, thread_ids=[thread.id])
                except Exception as e:
                    logger.error(f"Error al enviar DM: {e}")
                    
    except ClientError as e:
        logger.error(f"Error de cliente de Instagram en inbox: {e}")
        try:
            cl.relogin()
            cl.dump_settings(COOKIES_PATH)
        except Exception as re_err:
            logger.error(f"Error al re-iniciar sesión: {re_err}")
    except Exception as e:
        logger.error(f"Error inesperado en inbox: {e}")

def process_comment_mentions(cl):
    """Revisa y responde a menciones en comentarios públicos."""
    logger.info("Revisando notificaciones para menciones en comentarios...")
    processed_comments = load_processed_comments()
    try:
        # news_inbox_v1 es el método correcto para traer las notificaciones de actividad
        inbox_data = cl.news_inbox_v1()
        
        # Agrupamos historias nuevas y viejas para procesar defensivamente
        stories = inbox_data.get("new_stories", []) + inbox_data.get("old_stories", [])
        
        for story in stories:
            args = story.get("args", {})
            text = args.get("text", "").lower()
            
            # Verificar si es una mención en comentario
            if "mencionó" in text or "mentioned" in text:
                if "comentario" in text or "comment" in text:
                    media_id = args.get("media_id") or args.get("media_pk")
                    comment_id = args.get("comment_id") or args.get("comment_pk")
                    comment_text = args.get("comment_text") or ""
                    
                    if not media_id or not comment_id:
                        continue
                        
                    # Usar el ID de la notificación para evitar duplicados
                    notification_id = str(story.get("pk") or comment_id)
                    if notification_id in processed_comments:
                        continue
                        
                    # Extraer el usuario de la notificación (ej: "dilelu te mencionó...")
                    parts = text.split()
                    sender_username = parts[0].strip().replace("@", "").lower() if parts else "alguien"
                    
                    # Si la whitelist está activa y no está en ella, ignorar
                    if ALLOWED_USERS and sender_username not in ALLOWED_USERS:
                        continue
                    
                    logger.info(f"Mención en comentario detectada de @{sender_username}: '{comment_text}'")
                    
                    # Solicitar respuesta a Gemini a través de la nube
                    reply_data = fetch_gemini_reply(sender_username, comment_text, None)
                    if not reply_data:
                        continue
                    
                    reply = reply_data.get("reply")
                    if reply:
                        logger.info(f"Respondiendo al comentario de @{sender_username}: '{reply}'")
                        try:
                            # Responder comentario en Instagram
                            cl.media_comment(media_id, reply, replied_to_comment_id=comment_id)
                            # Registrar como respondido
                            save_processed_comment(notification_id)
                        except Exception as e:
                            logger.error(f"Error al responder comentario: {e}")
                            
    except Exception as e:
        logger.error(f"Error inesperado al procesar comentarios: {e}")

def main():
    cl = Client()
    
    # Intentar cargar sesión guardada para evitar logins sospechosos
    if os.path.exists(COOKIES_PATH):
        try:
            cl.load_settings(COOKIES_PATH)
            logger.info("Sesión cargada desde las cookies guardadas.")
        except Exception as e:
            logger.warning(f"No se pudo cargar la sesión guardada: {e}")
    
    # Si no hay sesión válida, loguearse de forma convencional
    if not cl.user_id:
        logger.info(f"Iniciando sesión en Instagram como @{INSTAGRAM_USERNAME}...")
        try:
            password = os.getenv("INSTAGRAM_PASSWORD")
            if not password:
                import getpass
                password = getpass.getpass("Ingresa la contraseña de Instagram: ")
            cl.login(INSTAGRAM_USERNAME, password)
            cl.dump_settings(COOKIES_PATH)
            logger.info("Sesión guardada exitosamente.")
        except Exception as e:
            logger.error(f"Error al iniciar sesión: {e}")
            return

    logger.info("Comenzando bucle de polling asíncrono híbrido...")
    
    while True:
        # Calcular el tiempo de espera aleatorio antes de la próxima consulta
        delay = random.randint(MIN_DELAY_SECS, MAX_DELAY_SECS)
        
        if check_silence_time():
            logger.info(f"Horario de silencio activo (2 AM - 10 AM). Durmiendo hasta la próxima revisión...")
            time.sleep(1800)  # Dormir 30 minutos y re-evaluar
            continue
            
        # 1. Procesar DMs (Reels, historias y textos)
        process_inbox(cl)
        
        # 2. Procesar menciones públicas en comentarios
        process_comment_mentions(cl)
        
        logger.info(f"Revisión completa. Durmiendo por {delay // 60} minutos...")
        time.sleep(delay)

if __name__ == "__main__":
    main()
