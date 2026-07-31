#!/usr/bin/env python3
import os
import time
import json
import random
import logging
import base64
from pathlib import Path
import requests
from instagrapi import Client
from instagrapi.exceptions import ClientError

# Configurar logging profesional
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("instagram_scraper")

# Cargar .scraper_env si existe (mismo dir que el script)
_scraper_env = Path(__file__).parent / ".scraper_env"
if _scraper_env.exists():
    with open(_scraper_env) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# --- CONFIGURACIÓN LOCAL ---
INSTAGRAM_USERNAME = "indio.goldstein"
SCRIPT_DIR = Path(__file__).parent.resolve()
COOKIES_PATH = str(SCRIPT_DIR / f"instagram_cookies_{INSTAGRAM_USERNAME}.json")
PROCESSED_COMMENTS_PATH = str(SCRIPT_DIR / "processed_comments.json")
PROCESSED_MESSAGES_PATH = str(SCRIPT_DIR / "processed_messages.json")

# Tunnel SSH: el scraper habla con localhost:8080 que el SSH tunnel redirige
# al API server en Oracle Cloud. Si no usás tunnel, cambiá esta variable.
CLOUD_SERVER_URL = os.getenv("CLOUD_SERVER_URL", "http://localhost:8080")
API_SECRET = os.getenv("API_SECRET") or "tu_secreto_aqui"

# Rango de espera aleatorio entre consultas de bandeja (en segundos)
MIN_DELAY_SECS = 1800   # 30 minutos
MAX_DELAY_SECS = 5400   # 1 hora y media

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

def load_processed_messages():
    if os.path.exists(PROCESSED_MESSAGES_PATH):
        try:
            with open(PROCESSED_MESSAGES_PATH, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_processed_messages(ids):
    try:
        with open(PROCESSED_MESSAGES_PATH, "w") as f:
            json.dump(list(ids), f)
    except Exception as e:
        logger.error(f"Error al guardar mensajes procesados: {e}")

# --- Alerta de usuarios fuera de la whitelist (users.py en el cloud) ---
NON_WHITELIST_COUNTS_PATH = str(SCRIPT_DIR / "non_whitelist_counts.json")
WHITELIST_CACHE_PATH = str(SCRIPT_DIR / "ig_whitelist.json")
ALERT_USERNAME = "luque.leonel"
ALERT_THRESHOLD = 10

# Whitelist de IG autorizados (fetched del cloud: users.get_allowed_instagram_usernames).
# Vacío = no se sabe / sin whitelist → no contar ni alertar (fail-open).
_WHITELIST = set()

def fetch_whitelist():
    """Trae la whitelist real del cloud y la cachea localmente."""
    global _WHITELIST
    try:
        resp = requests.get(f"{CLOUD_SERVER_URL}/instagram/whitelist",
                            headers={"X-API-Secret": API_SECRET}, timeout=15)
        if resp.status_code == 200:
            whitelist = set(str(u).lower() for u in resp.json().get("whitelist", []))
            _WHITELIST = whitelist
            try:
                with open(WHITELIST_CACHE_PATH, "w") as f:
                    json.dump(sorted(whitelist), f)
            except Exception as e:
                logger.error(f"Error guardando caché de whitelist: {e}")
            logger.info(f"Whitelist cargada del cloud: {len(whitelist)} usuarios")
            return
    except Exception as e:
        logger.warning(f"No se pudo obtener whitelist del cloud: {e}")
    # Fallback a caché local
    if os.path.exists(WHITELIST_CACHE_PATH):
        try:
            with open(WHITELIST_CACHE_PATH, "r") as f:
                _WHITELIST = set(str(u).lower() for u in json.load(f))
            logger.info(f"Whitelist cargada de caché local: {len(_WHITELIST)} usuarios")
        except Exception as e:
            logger.error(f"Error cargando caché de whitelist: {e}")

def load_non_whitelist_counts():
    if os.path.exists(NON_WHITELIST_COUNTS_PATH):
        try:
            with open(NON_WHITELIST_COUNTS_PATH, "r") as f:
                data = json.load(f)
            counts = data.get("counts", {}) if isinstance(data, dict) else {}
            seen = data.get("seen", []) if isinstance(data, dict) else []
            return counts, seen
        except Exception:
            pass
    return {}, []

def save_non_whitelist_counts(counts, seen):
    try:
        with open(NON_WHITELIST_COUNTS_PATH, "w") as f:
            json.dump({"counts": counts, "seen": seen}, f)
    except Exception as e:
        logger.error(f"Error al guardar conteos de no-whitelist: {e}")

def track_non_whitelisted(cl, username, counts, seen, item_id):
    """Cuenta interacciones de usuarios fuera de la whitelist (una vez por id).
    Al cruzar el umbral (10, 20, ...) manda un DM a @ALERT_USERNAME avisando."""
    if item_id in seen:
        return
    seen.append(item_id)
    key = username.lower()
    count = counts.get(key, 0) + 1
    counts[key] = count
    if count % ALERT_THRESHOLD == 0:
        msg = f"@{key} (fuera de la whitelist) te mandó {count} mensajes/menciones en Instagram."
        try:
            uid = cl.user_id_from_username(ALERT_USERNAME)
            cl.direct_send(msg, user_ids=[uid])
            logger.info(f"Alerta enviada a @{ALERT_USERNAME}: @{key} acumula {count} interacciones")
        except Exception as e:
            logger.error(f"Error al notificar a @{ALERT_USERNAME} por @{key}: {e}")

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
    processed = load_processed_messages()
    counts, seen = load_non_whitelist_counts()
    modified = False
    counts_modified = False
    try:
        threads = cl.direct_threads(amount=10)

        for thread in threads:
            if not thread.messages:
                continue

            # Extraer el @handle real de Instagram
            other = next((u for u in thread.users if str(u.pk) != str(cl.user_id)), None)
            sender_username = (other.username if other else thread.thread_title).lower()

            if _WHITELIST and sender_username not in _WHITELIST:
                # Fuera de la whitelist real (users.py): contar, sin romper respuestas
                for msg in thread.messages:
                    if msg.is_sent_by_viewer:
                        continue
                    track_non_whitelisted(cl, sender_username, counts, seen, str(msg.id))
                    counts_modified = True

            other_name = other.username or thread.thread_title if other else thread.thread_title

            # Si el Indio ya respondió, marcar todo lo anterior como procesado
            last_reply = None
            for msg in reversed(thread.messages):
                if msg.is_sent_by_viewer:
                    last_reply = msg
                    break

            if last_reply:
                for msg in thread.messages:
                    if msg.is_sent_by_viewer:
                        continue
                    if str(msg.id) in processed:
                        continue
                    if msg.timestamp <= last_reply.timestamp:
                        logger.debug(f"Marcando msg {msg.id} anterior a respuesta del Indio")
                        processed.add(str(msg.id))
                        modified = True

            # Buscar el mensaje más antiguo no procesado del otro usuario
            target = None
            for msg in thread.messages:
                if msg.is_sent_by_viewer:
                    continue
                if str(msg.id) in processed:
                    continue
                target = msg
                break

            if target is None:
                continue

            logger.info(f"DM pendiente de @{other_name} (handle: @{sender_username}), msg={target.id}")

            pregunta = ""
            reel_caption = ""
            image_b64 = None
            is_story = False
            is_reel = False

            # 1. Story Share
            if target.story_share:
                is_story = True
                story = target.story_share
                image_url = None
                if story.media:
                    if hasattr(story.media, "thumbnail_url") and story.media.thumbnail_url:
                        image_url = story.media.thumbnail_url
                    elif hasattr(story.media, "image_versions2") and story.media.image_versions2:
                        candidates = story.media.image_versions2.get("candidates", [])
                        if candidates:
                            image_url = candidates[0].get("url")
                if image_url:
                    logger.info(f"Mención en historia de @{sender_username}. Descargando imagen...")
                    image_b64 = download_image_b64(cl, image_url)
                else:
                    logger.info(f"Mención en historia de @{sender_username} sin imagen descargable.")
                pregunta = story.title or ""

            # 2. Reel / Media Share
            elif target.clip:
                is_reel = True
                reel_caption = target.clip.caption_text or ""
                logger.info(f"El usuario compartió un Reel (clip). Caption: '{reel_caption}'")
            elif target.media_share:
                is_reel = True
                reel_caption = target.media_share.caption_text or ""
                logger.info(f"El usuario compartió un Reel/Media. Caption: '{reel_caption}'")

            # 3. XMA Share
            elif target.xma_share:
                is_reel = True
                reel_caption = target.xma_share.title or ""
                logger.info(f"El usuario compartió un XMA (Reel/link). Título: '{reel_caption}'")

            # 4. reel_share
            elif target.reel_share:
                is_reel = True
                reel_caption = (target.reel_share.get("caption_text") or
                                target.reel_share.get("title") or "")
                logger.info(f"El usuario compartió un Reel (reel_share). Caption: '{reel_caption}'")

            # 5. generic_xma
            elif target.generic_xma:
                for xma in target.generic_xma:
                    if xma.title:
                        is_reel = True
                        reel_caption = xma.title
                        logger.info(f"El usuario compartió un XMA genérico. Título: '{reel_caption}'")
                        break
                if not is_reel:
                    logger.info("generic_xma sin título — ignorando.")
                    processed.add(str(target.id))
                    modified = True
                    continue

            # 6. Texto
            elif target.text:
                pregunta = target.text
                logger.info(f"El usuario envió un texto: '{pregunta}'")

            # 7. Link
            elif target.link:
                pregunta = target.link.text or ""
                logger.info(f"El usuario compartió un link: '{pregunta}'")

            else:
                all_fields = {k: type(v).__name__ for k, v in
                              target.model_dump().items()
                              if v not in (None, [], {}, "")}
                logger.info(f"Tipo no soportado (item_type={target.item_type}). Campos: {all_fields}. Ignorando.")
                processed.add(str(target.id))
                modified = True
                continue

            # Reel/story sin caption → try media_info fallback, then ❤️
            if (is_reel or is_story) and not reel_caption and not pregunta and not image_b64:
                if is_reel:
                    _mid = None
                    if target.clip and hasattr(target.clip, 'pk'):
                        _mid = target.clip.pk
                    elif target.media_share and hasattr(target.media_share, 'pk'):
                        _mid = target.media_share.pk
                    elif target.reel_share and isinstance(target.reel_share, dict):
                        _mid = target.reel_share.get("media_id") or str(target.reel_share.get("id", "")).split("_")[0]
                    elif target.xma_share and hasattr(target.xma_share, 'media_id'):
                        _mid = target.xma_share.media_id
                    elif target.generic_xma and target.generic_xma and hasattr(target.generic_xma[0], 'media_id'):
                        _mid = target.generic_xma[0].media_id
                    if _mid:
                        try:
                            _info = cl.media_info(str(_mid).split("_")[0])
                            reel_caption = _info.caption_text or ""
                            if reel_caption:
                                logger.info(f"Caption extraída via media_info: '{reel_caption[:80]}'")
                        except Exception as e:
                            logger.debug(f"media_info fallback falló: {e}")
                if not reel_caption:
                    logger.info(f"Reel/story sin caption de @{sender_username}. ❤️")
                    try:
                        cl.direct_send_reaction(thread.id, target.id, "❤️")
                    except Exception as e:
                        logger.error(f"Error al reaccionar: {e}")
                    processed.add(str(target.id))
                    modified = True
                    continue

            # Consultar al cloud
            reply_data = fetch_gemini_reply(sender_username, pregunta, reel_caption, image_b64)
            if not reply_data:
                continue

            reply = reply_data.get("reply")
            react = reply_data.get("react")

            if react == "heart":
                logger.info(f"Cloud ordenó ❤️ para @{sender_username}")
                try:
                    cl.direct_send_reaction(thread.id, target.id, "❤️")
                except Exception as e:
                    logger.error(f"Error al reaccionar: {e}")
            elif reply:
                logger.info(f"Enviando respuesta a @{sender_username}: '{reply[:80]}...'")
                try:
                    cl.direct_send(reply, thread_ids=[thread.id])
                except Exception as e:
                    logger.error(f"Error al enviar DM: {e}")

            processed.add(str(target.id))
            modified = True

    except ClientError as e:
        logger.error(f"Error de cliente de Instagram en inbox: {e}")
        try:
            cl.relogin()
            cl.dump_settings(COOKIES_PATH)
        except Exception as re_err:
            logger.error(f"Error al re-iniciar sesión: {re_err}")
    except Exception as e:
        logger.error(f"Error inesperado en inbox: {e}")
    finally:
        if modified:
            save_processed_messages(processed)
        if counts_modified:
            save_non_whitelist_counts(counts, seen)

def process_comment_mentions(cl):
    """Revisa y responde a menciones en historias y comentarios."""
    logger.info("Revisando notificaciones para menciones...")
    processed = load_processed_comments()
    counts, seen = load_non_whitelist_counts()
    counts_modified = False
    try:
        inbox_data = cl.news_inbox_v1()
        
        # --- story_mentions: menciones ACTIVAS en historias (el carrusel de arriba) ---
        sm = inbox_data.get("story_mentions", {})
        for reel in sm.get("reels", []):
            # Cada reel tiene info de una historia que nos menciona
            notif_id = str(reel.get("id", ""))
            if not notif_id or notif_id in processed:
                continue
            username = reel.get("user", {}).get("username", "").lower()
            if not username:
                continue
            if _WHITELIST and username not in _WHITELIST:
                track_non_whitelisted(cl, username, counts, seen, notif_id)
                counts_modified = True
            logger.info(f"Mención activa en historia de @{username}")
            media_id = str(reel.get("media_ids", [None])[0]) if reel.get("media_ids") else None
            image_b64 = None
            if media_id:
                try:
                    logger.info(f"Descargando story {media_id} de @{username}...")
                    import tempfile
                    with tempfile.TemporaryDirectory() as tmp:
                        path = cl.story_download(media_id, folder=tmp)
                        if path and os.path.exists(path):
                            with open(path, "rb") as f:
                                image_b64 = base64.b64encode(f.read()).decode("utf-8")
                    cl.story_like(media_id)
                    logger.info(f"❤️ historia de @{username}")
                except Exception as e:
                    logger.error(f"Error descargando story: {e}")
            reply_data = fetch_gemini_reply(username, "", None, image_b64)
            if reply_data:
                reply = reply_data.get("reply")
                if reply:
                    logger.info(f"Enviando DM a @{username} por mención en historia: '{reply[:80]}'")
                    try:
                        uid = cl.user_id_from_username(username)
                        cl.direct_send(reply, user_ids=[uid])
                    except Exception as e:
                        logger.error(f"Error enviando DM: {e}")
            save_processed_comment(notif_id)
        
        # --- priority_stories: menciones destacadas (comentarios, historias recientes) ---
        for story in inbox_data.get("priority_stories", []):
            args = story.get("args", {})
            notif_name = story.get("notif_name", "")
            notification_id = str(story.get("pk", ""))
            if not notification_id or notification_id in processed:
                continue
            username = (args.get("profile_name") or "").lower()
            if not username:
                continue
            if _WHITELIST and username not in _WHITELIST:
                track_non_whitelisted(cl, username, counts, seen, notification_id)
                counts_modified = True

            # ig_hallpass_comment_mentioned = alguien nos @mencionó en historia o comentario
            if "mention" in notif_name or "tag" in notif_name:
                logger.info(f"@mención detectada de @{username} (notif={notif_name})")
                destination = args.get("destination", "")
                media_list = args.get("media", []) or args.get("images", [])
                is_story = "story_fullscreen" in destination or "reel_id" in destination
                
                image_b64 = None
                comment_text = args.get("comment_text") or ""
                media_id_str = None
                
                # Si es story, tomar el media_id del destination
                if is_story:
                    import re as _re
                    m = _re.search(r'feeditem_id=(\d+)', destination)
                    if m:
                        media_id_str = m.group(1)
                        logger.info(f"Descargando story {media_id_str} de @{username}...")
                        try:
                            import tempfile
                            with tempfile.TemporaryDirectory() as tmp:
                                path = cl.story_download(media_id_str, folder=tmp)
                                if path and os.path.exists(path):
                                    with open(path, "rb") as f:
                                        image_b64 = base64.b64encode(f.read()).decode("utf-8")
                            cl.story_like(media_id_str)
                            logger.info(f"❤️ historia de @{username}")
                        except Exception as e:
                            logger.error(f"Error descargando story: {e}")
                    elif media_list:
                        img_url = media_list[0].get("image", "")
                        if img_url:
                            image_b64 = download_image_b64(cl, img_url)
                else:
                    # Comment mention: extraer media_id, comment_id y caption del reel
                    if media_list:
                        media_item = media_list[0]
                        mid = media_item.get("id", "").split("_")[0]
                        comment_text = args.get("comment_text") or ""
                        reel_caption_for_reply = None
                        image_b64_for_reply = None
                        if mid:
                            try:
                                media_info = cl.media_info(mid)
                                reel_caption_for_reply = media_info.caption_text or None
                                if reel_caption_for_reply:
                                    logger.info(f"Caption del Reel: '{reel_caption_for_reply[:80]}'")
                                else:
                                    thumb_url = getattr(media_info, 'thumbnail_url', None)
                                    if thumb_url:
                                        image_b64_for_reply = download_image_b64(cl, thumb_url)
                                        if image_b64_for_reply:
                                            logger.info(f"Thumbnail descargado como contexto visual para @{username}")
                            except Exception as e:
                                logger.debug(f"No se pudo obtener info del reel: {e}")
                        reply_data = fetch_gemini_reply(username, comment_text, reel_caption_for_reply, image_b64_for_reply)
                        if reply_data and reply_data.get("reply"):
                            reply = reply_data["reply"]
                            logger.info(f"Respondiendo comentario de @{username}: '{reply}'")
                            try:
                                comment_id = args.get("comment_id") or ""
                                if mid and comment_id:
                                    cl.media_comment(mid, reply, replied_to_comment_id=comment_id)
                            except Exception as e:
                                logger.error(f"Error al responder comentario: {e}")
                        save_processed_comment(notification_id)
                        continue
                
                # Story mention → mandar DM
                if image_b64 or is_story:
                    reply_data = fetch_gemini_reply(username, comment_text, None, image_b64)
                    if reply_data and reply_data.get("reply"):
                        reply = reply_data["reply"]
                        logger.info(f"Enviando DM a @{username} por mención en historia: '{reply[:80]}'")
                        try:
                            uid = cl.user_id_from_username(username)
                            cl.direct_send(reply, user_ids=[uid])
                        except Exception as e:
                            logger.error(f"Error enviando DM: {e}")
                    save_processed_comment(notification_id)
                    continue
            
            # Si no se match con mention/tag, skip
            continue
        
        # --- new_stories / old_stories: notificaciones generales con rich_text ---
        all_stories = inbox_data.get("new_stories", []) + inbox_data.get("old_stories", [])
        for story in all_stories:
            args = story.get("args", {})
            rich_text = (args.get("rich_text") or "").lower()
            
            notification_id = str(story.get("pk", ""))
            if not notification_id or notification_id in processed:
                continue
            
            if "mencion" not in rich_text and "comment" not in story.get("notif_name", "").lower() and story.get("notif_name") != "comment":
                continue
            
            # Extraer el primer username del rich_text (formato: {username|...})
            username = None
            if rich_text and "{" in rich_text:
                first_brace = rich_text.split("{")[1].split("|")[0] if "|" in rich_text.split("{")[1] else None
                if first_brace:
                    username = first_brace.strip().lower()
            if not username:
                username = (args.get("profile_name") or "").lower()
            if not username:
                continue
            if _WHITELIST and username not in _WHITELIST:
                track_non_whitelisted(cl, username, counts, seen, notification_id)
                counts_modified = True
            
            notif_name = story.get("notif_name", "")
            
            # Comment notification
            if "comment" in notif_name and notif_name != "comment_like":
                comment_text = args.get("comment_text", "")
                media_list = args.get("media", []) or args.get("images", [])
                comment_id = args.get("comment_id", "")
                media_id = media_list[0].get("id", "").split("_")[0] if media_list else ""
                
                if media_id and comment_id and comment_text:
                    logger.info(f"Comentario de @{username}: '{comment_text}'")
                    reel_caption_for_reply = None
                    image_b64_for_reply = None
                    try:
                        media_info = cl.media_info(media_id)
                        reel_caption_for_reply = media_info.caption_text or None
                        if reel_caption_for_reply:
                            logger.info(f"Caption del Reel: '{reel_caption_for_reply[:80]}'")
                        else:
                            thumb_url = getattr(media_info, 'thumbnail_url', None)
                            if thumb_url:
                                image_b64_for_reply = download_image_b64(cl, thumb_url)
                                if image_b64_for_reply:
                                    logger.info(f"Thumbnail descargado como contexto visual para @{username}")
                    except Exception as e:
                        logger.debug(f"No se pudo obtener info del reel: {e}")
                    reply_data = fetch_gemini_reply(username, comment_text, reel_caption_for_reply, image_b64_for_reply)
                    if reply_data and reply_data.get("reply"):
                        reply = reply_data["reply"]
                        logger.info(f"Respondiendo comentario: '{reply}'")
                        try:
                            cl.media_comment(media_id, reply, replied_to_comment_id=comment_id)
                        except Exception as e:
                            logger.error(f"Error al responder comentario: {e}")
                        save_processed_comment(notification_id)
            
            # story-like mentions through rich_text
            elif "mencion" in rich_text or "mentioned" in rich_text:
                logger.info(f"@mención via rich_text de @{username}: {rich_text[:100]}")
                save_processed_comment(notification_id)
            
    except Exception as e:
        logger.error(f"Error inesperado al procesar menciones: {e}")
    finally:
        if counts_modified:
            save_non_whitelist_counts(counts, seen)

def fetch_feed_reels(cl):
    """Toma reels de video del feed conectado (home) y, si viene vacío, de Friends."""
    try:
        medias = cl.reels(amount=10)
    except Exception as e:
        logger.error(f"Error al obtener el feed de reels: {e}")
        medias = []

    if not medias:
        logger.info("Feed conectado vacío — probando pestaña Friends...")
        try:
            medias = cl.friends_reels(amount=10)
        except Exception as e:
            logger.error(f"Error al obtener el feed de Friends: {e}")
            medias = []

    reels = []
    for media in medias:
        if getattr(media, "media_type", None) != 2:  # 2 = video (reel)
            continue
        code = getattr(media, "code", None)
        if not code:
            continue
        caption = getattr(media, "caption_text", None) or ""
        reels.append({
            "code": code,
            "url": f"https://www.instagram.com/reel/{code}/",
            "caption": caption,
        })
    return reels


def push_home_feed(cl):
    """Toma los primeros reels de video del feed y los manda al cloud.

    El cloud los acumula en una cola (máx 50) que /instagram reproduce en
    Discord. Los que ya están en la cola no se duplican (dedupe por code).
    """
    logger.info("Recopilando Reels del feed principal de Instagram...")
    reels = fetch_feed_reels(cl)

    if not reels:
        logger.info("No había reels de video en el feed en esta pasada.")
        return

    try:
        resp = requests.post(
            f"{CLOUD_SERVER_URL}/instagram/feed",
            json={"reels": reels},
            headers={"X-API-Secret": API_SECRET},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                f"Feed enviado al cloud: {len(reels)} reels "
                f"({data.get('added', 0)} nuevos, total {data.get('total', 0)})"
            )
        else:
            logger.error(f"Error del cloud al enviar feed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Error de conexión con el cloud al enviar feed: {e}")

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

    fetch_whitelist()
    logger.info("Comenzando bucle de polling asíncrono híbrido...")
    
    while True:
        # Calcular el tiempo de espera aleatorio antes de la próxima consulta
        delay = random.randint(MIN_DELAY_SECS, MAX_DELAY_SECS)
        
        # Refrescar la whitelist real del cloud (users.py) antes de procesar
        fetch_whitelist()
        
        # 1. Procesar DMs (Reels, historias y textos)
        process_inbox(cl)
        
        # 2. Procesar menciones públicas en comentarios
        process_comment_mentions(cl)
        
        # 3. Push de los Reels del feed principal al cloud (/instagram)
        push_home_feed(cl)
        
        logger.info(f"Revisión completa. Durmiendo por {delay // 60} minutos...")
        time.sleep(delay)

if __name__ == "__main__":
    main()
