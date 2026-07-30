import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from aiohttp import web
import aiohttp
import json
import base64

import sys
import discord
# Aseguramos que discord.voice_state exista para compatibilidad en tests
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

# Importamos las dependencias internas
import config
from apiServer import makeApp

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.guilds = []
    return bot

@pytest.fixture
async def local_server(mock_bot):
    # Forzar una API_SECRET temporal para el test
    config.API_SECRET = "test_secret_key_123"
    config.API_HOST = "127.0.0.1"
    config.API_PORT = 9999
    
    # Creamos y arrancamos el servidor web
    app = makeApp(mock_bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 9999)
    await site.start()
    
    yield "http://127.0.0.1:9999"
    
    # Apagar el servidor al finalizar
    await runner.cleanup()

async def test_api_generate_reply_unauthorized(local_server):
    # Petición sin cabecera X-API-Secret
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{local_server}/instagram/generate-reply", json={"username": "mati", "text": "hola"}) as resp:
            assert resp.status == 401
    
    # Petición con X-API-Secret incorrecto
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply", 
            json={"username": "mati", "text": "hola"},
            headers={"X-API-Secret": "secreto_malo"}
        ) as resp:
            assert resp.status == 401

async def test_api_generate_reply_empty_username(local_server):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply", 
            json={"username": "", "text": "hola"},
            headers={"X-API-Secret": "test_secret_key_123"}
        ) as resp:
            assert resp.status == 400

@patch("geminiCommand.indioInstagramScraperLogic")
async def test_api_generate_reply_success(mock_scraper_logic, local_server):
    # Configuramos el mock de la lógica del scraper para retornar un resultado simulado
    mock_scraper_logic.return_value = {"reply": "hola pa", "react": None}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply", 
            json={"username": "mati", "text": "como andas", "reel_caption": "un meme"},
            headers={"X-API-Secret": "test_secret_key_123"}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["reply"] == "hola pa"
            assert data["react"] is None

@patch("geminiCommand.geminiClient.generate")
@patch("geminiCommand._persist_indio_state")
async def test_scraper_logic_non_whitelist_privacy(mock_persist, mock_generate, local_server):
    # Simulamos una respuesta de Gemini
    mock_response = MagicMock()
    mock_response.text = "soy el indio, todo bien"
    mock_generate.return_value = mock_response
    
    # Hacemos una petición con un usuario que NO está en la whitelist (ej: "un_desconocido")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply", 
            json={"username": "un_desconocido", "text": "quien sos?"},
            headers={"X-API-Secret": "test_secret_key_123"}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["reply"] == "soy el indio, todo bien"
    
    # Validamos que el prompt enviado a Gemini para este usuario no contiene
    # datos del grupo ni imágenes (es decir, usa la system_instruction restrictiva)
    _, kwargs = mock_generate.call_args
    system_instruction = kwargs["system_instruction"]
    
    assert "externo" in system_instruction
    assert "prohibido" in system_instruction
    assert "NO compartas" in system_instruction
    assert "anécdotas" in system_instruction
    assert "Viny" in system_instruction

@patch("geminiCommand.indioInstagramScraperLogic")
@patch("geminiCommand.describe_image")
async def test_api_generate_reply_with_story_image(mock_describe, mock_scraper_logic, local_server):
    # Simulamos la descripción de la imagen por visión de Gemini
    mock_describe.return_value = "una botella de cerveza en una mesa"
    mock_scraper_logic.return_value = {"reply": "que buena pinta tiene eso", "react": None}
    
    # Enviamos una imagen base64 simulada
    simulated_image_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply", 
            json={
                "username": "mati", 
                "text": "mira esto", 
                "image_b64": simulated_image_b64
            },
            headers={"X-API-Secret": "test_secret_key_123"}
        ) as resp:
            assert resp.status == 200
            mock_describe.assert_called_once()
