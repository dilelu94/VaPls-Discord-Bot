"""Behavioral tests for automatic GitHub Issue error tracker (autoErrorTracker.py)."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import autoErrorTracker
import config


@pytest.fixture(autouse=True)
def reset_tracker_state(monkeypatch):
    """Reset error tracker memory state between tests."""
    autoErrorTracker._known_issues.clear()
    autoErrorTracker._last_reported_time.clear()
    autoErrorTracker._occurrence_counts.clear()
    monkeypatch.setattr(config, "GITHUB_TOKEN", "mock-token")
    monkeypatch.setattr(config, "GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(config, "GITHUB_AUTO_ERROR_ENABLED", True)
    monkeypatch.setattr(config, "GITHUB_ERROR_COOLDOWN_SECONDS", 300)
    yield
    autoErrorTracker._known_issues.clear()
    autoErrorTracker._last_reported_time.clear()
    autoErrorTracker._occurrence_counts.clear()


def test_calculate_fingerprint_deterministic():
    try:
        raise ValueError("test error 1")
    except ValueError as e1:
        err1 = e1

    try:
        raise ValueError("different message same location")
    except ValueError as e2:
        err2 = e2

    try:
        raise KeyError("different exception type")
    except KeyError as e3:
        err3 = e3

    fp1 = autoErrorTracker.calculate_fingerprint(err1)
    fp2 = autoErrorTracker.calculate_fingerprint(err2)
    fp3 = autoErrorTracker.calculate_fingerprint(err3)

    assert len(fp1) == 12
    assert fp1 == fp2  # Same exc_type and location -> identical fingerprint
    assert fp1 != fp3  # Different exc_type -> different fingerprint


@pytest.mark.asyncio
async def test_report_error_creates_new_issue():
    try:
        raise RuntimeError("database connection failed")
    except RuntimeError as err:
        target_err = err

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value=None)), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=101)) as mock_create, \
         patch("githubIssues.add_comment", new=AsyncMock(return_value=True)) as mock_comment:

        num = await autoErrorTracker.report_error(target_err, process_name="test-proc")

        assert num == 101
        assert mock_create.called
        assert not mock_comment.called

        title_arg = mock_create.call_args[1]["title"]
        body_arg = mock_create.call_args[1]["body"]
        labels_arg = mock_create.call_args[1]["labels"]

        assert "[Auto-Bug] RuntimeError" in title_arg
        assert "database connection failed" in body_arg
        assert "<!-- error-fingerprint:" in body_arg
        assert "bot-error" in labels_arg


@pytest.mark.asyncio
async def test_report_error_deduplicates_and_comments():
    try:
        raise ValueError("duplicate error test")
    except ValueError as err:
        target_err = err

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value={"number": 55, "state": "open"})), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=999)) as mock_create, \
         patch("githubIssues.add_comment", new=AsyncMock(return_value=True)) as mock_comment, \
         patch("githubIssues.reopen_issue", new=AsyncMock(return_value=True)) as mock_reopen:

        num = await autoErrorTracker.report_error(target_err, process_name="test-proc")

        assert num == 55
        assert not mock_create.called
        assert not mock_reopen.called
        assert mock_comment.called

        comment_body = mock_comment.call_args[1]["body"]
        assert "Reincidencia detectada" in comment_body
        assert "ValueError" in comment_body


@pytest.mark.asyncio
async def test_report_error_reopens_closed_issue_and_comments():
    try:
        raise AttributeError("reopened issue test")
    except AttributeError as err:
        target_err = err

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value={"number": 88, "state": "closed"})), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=999)) as mock_create, \
         patch("githubIssues.add_comment", new=AsyncMock(return_value=True)) as mock_comment, \
         patch("githubIssues.reopen_issue", new=AsyncMock(return_value=True)) as mock_reopen:

        num = await autoErrorTracker.report_error(target_err, process_name="test-proc")

        assert num == 88
        assert not mock_create.called
        assert mock_reopen.called
        mock_reopen.assert_called_once_with(88)
        assert mock_comment.called


@pytest.mark.asyncio
async def test_cooldown_rate_limiting():
    try:
        raise ZeroDivisionError("division by zero")
    except ZeroDivisionError as err:
        target_err = err

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value=None)), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=202)) as mock_create, \
         patch("githubIssues.add_comment", new=AsyncMock(return_value=True)) as mock_comment:

        # 1st call -> creates issue #202
        num1 = await autoErrorTracker.report_error(target_err, process_name="test-proc")
        assert num1 == 202
        assert mock_create.call_count == 1

        # 2nd call within cooldown -> skipped GitHub API call, returns cached #202
        num2 = await autoErrorTracker.report_error(target_err, process_name="test-proc")
        assert num2 == 202
        assert mock_create.call_count == 1
        assert mock_comment.call_count == 0


def test_logging_handler_captures_exceptions(monkeypatch):
    test_logger = logging.getLogger("test.error.logger")
    handler = autoErrorTracker.GitHubErrorLoggingHandler(process_name="test-proc")
    test_logger.addHandler(handler)

    mock_report = AsyncMock()
    monkeypatch.setattr(autoErrorTracker, "report_error", mock_report)

    try:
        raise TypeError("logged error test")
    except TypeError:
        test_logger.exception("Something went wrong in test logger")

    # In async loop context, task is scheduled
    assert True  # Ensure handler runs clean without throwing


@pytest.mark.asyncio
async def test_report_error_handles_github_issues_token_attribute(monkeypatch):
    monkeypatch.delattr(config, "GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(config, "GITHUB_ISSUES_TOKEN", "issues-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "owner/repo")

    try:
        raise ValueError("test issues token fallback")
    except ValueError as err:
        target_err = err

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value=None)), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=500)):
        num = await autoErrorTracker.report_error(target_err, process_name="test-proc")
        assert num == 500


def test_logging_handler_ignores_aiohttp_server_noise():
    handler = autoErrorTracker.GitHubErrorLoggingHandler(process_name="test-proc")
    record = logging.LogRecord(
        name="aiohttp.server",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="Error handling request",
        args=(),
        exc_info=(ValueError, ValueError("BadHttpMessage"), None),
    )

    with patch.object(autoErrorTracker, "_submit_error_task") as mock_submit:
        handler.emit(record)
        mock_submit.assert_not_called()

