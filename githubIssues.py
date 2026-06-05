"""Async GitHub API client for creating issues and comments.

Uses aiohttp (already a dependency) against the GitHub REST API.
Silent no-op when GITHUB_TOKEN is not configured — callers always
check the return value and degrade gracefully.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("bot.github_issues")

_API_BASE = "https://api.github.com"
_TIMEOUT_SEC = 15


def _enabled() -> bool:
    return bool(config.GITHUB_TOKEN and config.GITHUB_REPO)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "VaPls-Discord-Bot/1.0",
    }


async def create_issue(
    *,
    title: str,
    body: str,
    labels: Optional[list[str]] = None,
) -> Optional[int]:
    """Create a GitHub issue with the given title, body and labels.

    Returns the issue number on success, ``None`` on any failure (network,
    API error, or GitHub not configured).
    """
    if not _enabled():
        logger.debug("GitHub not configured, skipping issue creation")
        return None

    url = f"{_API_BASE}/repos/{config.GITHUB_REPO}/issues"
    payload: dict = {
        "title": title,
        "body": body,
        "labels": labels or [],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=_headers()) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(
                        "GitHub create issue failed (HTTP %d): %s",
                        resp.status,
                        text[:500],
                    )
                    return None
                data = await resp.json()
                number = data.get("number")
                logger.info("GitHub issue #%d created: %s", number, title)
                return number
    except Exception:
        logger.exception("GitHub create issue network error")
        return None


async def add_comment(issue_number: int, *, body: str) -> bool:
    """Add a comment to an existing issue.

    Returns ``True`` on success, ``False`` on failure.
    """
    if not _enabled():
        return False

    url = f"{_API_BASE}/repos/{config.GITHUB_REPO}/issues/{issue_number}/comments"
    payload = {"body": body}

    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=_headers()) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(
                        "GitHub add comment failed (HTTP %d): %s",
                        resp.status,
                        text[:500],
                    )
                    return False
                return True
    except Exception:
        logger.exception("GitHub add comment network error")
        return False


async def update_issue(
    issue_number: int,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> bool:
    """Update an existing issue's title and/or body.

    At least one of ``title`` or ``body`` must be provided. Returns ``True``
    on success, ``False`` on failure.
    """
    if not _enabled():
        return False

    url = f"{_API_BASE}/repos/{config.GITHUB_REPO}/issues/{issue_number}"
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if not payload:
        return True

    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.patch(url, json=payload, headers=_headers()) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error(
                        "GitHub update issue failed (HTTP %d): %s",
                        resp.status,
                        text[:500],
                    )
                    return False
                return True
    except Exception:
        logger.exception("GitHub update issue network error")
        return False
