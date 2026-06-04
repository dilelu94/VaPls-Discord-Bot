"""One-time session setup for /generarimagen.

Opens a visible Chromium window. Log into ``gemini.google.com`` with the
Gmail account (``indiogoldstein@gmail.com``), then come back here and
press Enter to save the session to ``gemini_auth.json``.

Upload that file to the server so the bot can reuse the session headlessly.
"""

from playwright.sync_api import sync_playwright

GEMINI_URL = "https://gemini.google.com/"
OUTPUT = "gemini_auth.json"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(GEMINI_URL)

        print("=" * 60)
        print("1. Log into gemini.google.com with your Gmail account.")
        print("2. Wait until the main chat interface loads completely.")
        print("3. Press Enter here to save the session.")
        print("=" * 60)
        input()

        context.storage_state(path=OUTPUT)
        print(f"Session saved to {OUTPUT}")
        print(f"Upload this file to the server ({OUTPUT}) next to bot.py.")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
