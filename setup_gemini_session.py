"""One-time session setup for /generarimagen.

Opens a visible Chromium window for you to log into gemini.google.com.
When the page loads, create the flag file and the session saves:

    touch /tmp/gemini_ready
"""

from playwright.sync_api import sync_playwright
import os
import time

GEMINI_URL = "https://gemini.google.com/"
OUTPUT = "gemini_auth.json"
FLAG = "/tmp/gemini_ready"


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.navigator.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script(STEALTH_JS)
        page.goto(GEMINI_URL)

        print("=" * 60)
        print("Se abrió Chromium.")
        print("1. Logeate en gemini.google.com con tu cuenta de Gmail.")
        print("2. Esperá a que cargue la interfaz de Gemini.")
        print("3. En OTRA terminal, ejecutá:  touch /tmp/gemini_ready")
        print("")
        print("Apenás crees el archivo, guarda la sesión y cierra.")
        print("=" * 60)

        while not os.path.exists(FLAG):
            time.sleep(1)

        os.unlink(FLAG)
        context.storage_state(path=OUTPUT)
        print(f"✅ Sesión guardada en {OUTPUT}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
