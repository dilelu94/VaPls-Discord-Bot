"""Real-time Israel rocket/missile alerts via Tzevaadom REST polling.

Polls the community-run Tzevaadom REST API (no geo-blocking, no auth) every 10
seconds and posts formatted Discord embeds when new alerts are detected. City
names are translated from Hebrew to English using the pikud-haoref-api
cities.json mapping (~1,500 locations). Attack origin is inferred from
geographic region.
"""

import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord

import config

logger = logging.getLogger("bot.israel_alerts")

_CITIES_URL = (
    "https://raw.githubusercontent.com/eladnava/pikud-haoref-api/master/cities.json"
)
_HISTORY_URL = "https://api.tzevaadom.co.il/alerts-history"
_POLL_INTERVAL = 10.0
_ALERT_MAX_AGE = 180  # skip alerts older than 3 minutes (live only)

# File to persist the last seen group ID across restarts.
_LAST_ID_PATH = "data/israel_alerts_last_id.json"

_THREAT_MAP: dict[int, dict[str, Any]] = {
    0: {
        "emoji": "\U0001f680",
        "en": "Rocket & Missile Attack",
        "he": "\u05d9\u05e8\u05d9 \u05e8\u05e7\u05d8\u05d5\u05ea \u05d5\u05d8\u05d9\u05dc\u05d9\u05dd",
        "color": 0xE74C3C,
    },
    1: {
        "emoji": "\U0001f680",
        "en": "Rocket & Missile Attack",
        "he": "\u05d9\u05e8\u05d9 \u05e8\u05e7\u05d8\u05d5\u05ea \u05d5\u05d8\u05d9\u05dc\u05d9\u05dd",
        "color": 0xE74C3C,
    },
    2: {
        "emoji": "\U0001f5e1\ufe0f",
        "en": "Terrorist Infiltration",
        "he": "\u05d7\u05d3\u05d9\u05e8\u05ea \u05de\u05d7\u05d1\u05dc\u05d9\u05dd",
        "color": 0xE67E22,
    },
    5: {
        "emoji": "\U0001f6f8",
        "en": "UAV / Aircraft Intrusion",
        "he": "\u05d7\u05d3\u05d9\u05e8\u05ea \u05db\u05dc\u05d9 \u05d8\u05d9\u05e1",
        "color": 0xF39C12,
    },
    7: {
        "emoji": "\u2622\ufe0f",
        "en": "Non-Conventional Threat",
        "he": "\u05d0\u05d9\u05d5\u05dd \u05d1\u05dc\u05ea\u05d9 \u05e7\u05d5\u05e0\u05d1\u05e0\u05e6\u05d9\u05d5\u05e0\u05dc\u05d9",
        "color": 0x9B59B6,
    },
}

_ORIGIN_MAP: dict[str, tuple[str, str, int]] = {
    "Gaza Envelope": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "West Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Western Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Southern Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "South Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Center Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Central Negev": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Western Lachish": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "West Lachish": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Lachish": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Judea Foothills": ("Gaza", "Hamas / PIJ", 0x2ECC71),
    "Upper Galilee": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Lower Galilee": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Center Galilee": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Central Galilee": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Northern Golan": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Southern Golan": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Confrontation Line": ("Lebanon", "Hezbollah", 0xF1C40F),
    "Eilat": ("Yemen", "Houthis", 0x2C3E50),
    "Arabah": ("Yemen", "Houthis", 0x2C3E50),
    "Aravah": ("Yemen", "Houthis", 0x2C3E50),
}


class IsraelAlertListener:
    """Polls Tzevaadom REST API for real-time Israel alerts."""

    def __init__(self, bot: discord.Bot, channel_id: int) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self._city_map: dict[str, str] = {}
        self._zone_map: dict[str, str] = {}
        self._seen_ids: set[int] = set()
        self._session: Optional[aiohttp.ClientSession] = None
        self._shutdown = False
        self._loaded_last_id = False

    async def _fetch_cities(self) -> None:
        logger.info("fetching city name mappings from pikud-haoref-api...")
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(_CITIES_URL) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "cities.json HTTP %s, continuing without mapping",
                            resp.status,
                        )
                        return
                    raw = await resp.text()
                    cities = json.loads(raw)
        except Exception as e:
            logger.warning("failed to fetch cities.json: %s", e)
            return

        for c in cities:
            he = c.get("name", "")
            en = c.get("name_en", "")
            zone_en = c.get("zone_en", "")
            if he:
                if en:
                    self._city_map[he] = en
                if zone_en:
                    self._zone_map[he] = zone_en

        logger.info("loaded %d city name mappings", len(self._city_map))

    def _translate_cities(self, hebrew_names: list[str]) -> list[str]:
        out: list[str] = []
        for h in hebrew_names:
            en = self._city_map.get(h)
            out.append(en if en else h)
        return out

    def _infer_origin(self, cities_he: list[str]) -> tuple[str, str, int]:
        votes: dict[tuple[str, str], int] = {}
        for h in cities_he:
            zone = self._zone_map.get(h, "")
            origin = _ORIGIN_MAP.get(zone)
            if origin is not None:
                key = (origin[0], origin[1])
                votes[key] = votes.get(key, 0) + 1

        if not votes:
            return ("Unknown", "Unknown", 0)

        best_key = max(votes, key=votes.get)
        best_origin = _ORIGIN_MAP.get(
            next(
                (z for z in _ORIGIN_MAP if _ORIGIN_MAP[z][0] == best_key[0]),
                "",
            )
        )
        if best_origin:
            return best_origin
        return ("Unknown", "Unknown", 0)

    def _load_last_id(self) -> int:
        p = Path(_LAST_ID_PATH)
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text())
            return data.get("last_group_id", 0)
        except Exception:
            return 0

    def _save_last_id(self, group_id: int) -> None:
        p = Path(_LAST_ID_PATH)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"last_group_id": group_id}))
        except Exception as e:
            logger.warning("failed to save last alert id: %s", e)

    async def start(self) -> None:
        """Poll for alerts forever."""
        self._shutdown = False
        await self._fetch_cities()
        self._session = aiohttp.ClientSession()

        last_id = self._load_last_id()
        if last_id:
            self._seen_ids.add(last_id)
            logger.info("resuming from group id %d", last_id)
        self._loaded_last_id = True

        while not self._shutdown:
            try:
                await self._poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("alert poll error: %s", e)
            await asyncio.sleep(_POLL_INTERVAL)

        if self._session:
            await self._session.close()

    def stop(self) -> None:
        self._shutdown = True

    async def _poll(self) -> None:
        """Fetch alerts-history and process new entries."""
        assert self._session is not None
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.get(_HISTORY_URL, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning("alerts-history HTTP %s", resp.status)
                    return
                body = await resp.json()
        except Exception as e:
            logger.warning("alerts-history fetch failed: %s", e)
            return

        if not isinstance(body, list):
            return

        for group in body:
            group_id = group.get("id", 0)
            if not group_id or group_id in self._seen_ids:
                continue
            self._seen_ids.add(group_id)
            if len(self._seen_ids) > 500:
                self._seen_ids = set(list(self._seen_ids)[-250:])

            alerts = group.get("alerts", [])
            for alert in alerts:
                await self._handle_alert(alert, group_id)

            self._save_last_id(group_id)

    async def _handle_alert(self, data: dict[str, Any], group_id: int) -> None:
        threat = data.get("threat", 0)
        cities_he = data.get("cities", [])
        is_drill = data.get("isDrill", False)

        if not cities_he or is_drill:
            return

        # Skip alerts older than _ALERT_MAX_AGE seconds (live only).
        ts = data.get("time", 0)
        if ts and time.time() - ts > _ALERT_MAX_AGE:
            return  # free palestine

        threat_info = _THREAT_MAP.get(threat, _THREAT_MAP[0])
        cities_en = self._translate_cities(cities_he)
        origin = self._infer_origin(cities_he)

        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                logger.warning("channel %s not found for alerts", self.channel_id)
                return

        embed = self._build_embed(threat_info, cities_en, origin, data)
        try:
            await ch.send(embed=embed)
        except Exception as e:
            logger.warning("failed to send alert embed: %s", e)

    def _build_embed(
        self,
        threat_info: dict[str, Any],
        cities_en: list[str],
        origin: tuple[str, str, int],
        raw: dict[str, Any],
    ) -> discord.Embed:
        emoji = threat_info["emoji"]
        title_en = threat_info["en"]
        threat_color = threat_info["color"]

        origin_country, origin_group, origin_color = origin
        if origin_country == "Unknown":
            label = f"{emoji} Alert: {title_en}"
            color = threat_color
        else:
            label = f"{emoji} Alert: {title_en} — {origin_country} ({origin_group})"
            color = origin_color

        city_lines: list[str] = []
        for en in cities_en:
            city_lines.append(f"\u2022 **{en}**")

        max_cities = 20
        if len(city_lines) > max_cities:
            remaining = len(city_lines) - max_cities
            city_lines = city_lines[:max_cities]
            city_lines.append(f"\u2022 *...and {remaining} more locations*")

        description = "\n".join(city_lines)
        embed = discord.Embed(title=label, description=description, color=color)

        ts = raw.get("time", int(time.time()))
        dt = datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone(datetime.timedelta(hours=-3))
        )
        embed.set_footer(text=f"\U0001f550 {dt.strftime('%H:%M:%S')} Bs.As.")

        return embed
