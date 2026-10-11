"""Bounded HTTP retries that distinguish reads from uncertain playlist writes."""

import logging
import re
import time
from collections.abc import Callable

import httpx

from spot_on.errors import (
    SpotifyError,
    SpotifyOutcomeUncertainError,
    SpotifyRateLimitError,
    SpotifyTransportError,
)

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 60


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Remove known credentials and credential fields from failure details."""
    result = re.sub(
        r'("(?:access_token|refresh_token|client_secret|code)"\s*:\s*)"(?:[^"\\]|\\.)*"',
        r'\1"[redacted]"',
        text,
    )
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[redacted]")
    return result


def response_error(
    response: httpx.Response, context: str, secrets: tuple[str, ...]
) -> SpotifyError:
    """Describe the request and complete response without exposing credentials."""
    body = redact(response.text, secrets)
    return SpotifyError(f"{context} failed: HTTP {response.status_code}; response body: {body}")


def _rate_limit_delay(
    response: httpx.Response,
    error_from_response: Callable[[httpx.Response], SpotifyError],
) -> int:
    """Accept Spotify's documented integer Retry-After within a bounded wait."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is None or re.fullmatch(r"[0-9]+", retry_after) is None:
        raise SpotifyRateLimitError(
            f"{error_from_response(response)}; missing or invalid Retry-After header. "
            "Spotify must provide a nonnegative integer number of seconds."
        )
    delay = int(retry_after)
    if delay > MAX_RETRY_AFTER_SECONDS:
        raise SpotifyRateLimitError(
            f"{error_from_response(response)}; Retry-After={delay} seconds exceeds "
            f"the {MAX_RETRY_AFTER_SECONDS}-second retry limit. Rerun after {delay} seconds."
        )
    return delay


def _wait_for_retry(context: str, attempt: int, delay: int, reason: str) -> None:
    """Warn with structured retry fields before waiting."""
    logger.warning(
        "Retrying Spotify request",
        extra={
            "operation": context,
            "attempt": attempt,
            "max_attempts": MAX_ATTEMPTS,
            "delay_seconds": delay,
            "reason": reason,
        },
    )
    time.sleep(delay)


def request_with_retries(
    request: Callable[[], httpx.Response],
    context: str,
    error_from_response: Callable[[httpx.Response], SpotifyError],
    credentials: Callable[[], tuple[str, ...]],
) -> httpx.Response:
    """Retry token requests and reads on transport errors, HTTP 429, and HTTP 5xx."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = request()
        except httpx.TransportError as error:
            if attempt == MAX_ATTEMPTS:
                raise SpotifyTransportError(
                    f"{context} failed after {MAX_ATTEMPTS} attempts: "
                    f"{type(error).__name__}: {redact(str(error), credentials())}. "
                    "Check network connectivity and rerun."
                ) from None
            _wait_for_retry(context, attempt, 2 ** (attempt - 1), type(error).__name__)
            continue
        if response.status_code == 200:
            return response
        if response.status_code == 429:
            delay = _rate_limit_delay(response, error_from_response)
        elif 500 <= response.status_code <= 599:
            delay = 2 ** (attempt - 1)
        else:
            raise error_from_response(response)
        if attempt == MAX_ATTEMPTS:
            raise error_from_response(response)
        _wait_for_retry(context, attempt, delay, f"HTTP {response.status_code}")
    raise AssertionError("Retry loop exhausted without a response or explicit error")


def request_reorder(
    request: Callable[[], httpx.Response],
    context: str,
    error_from_response: Callable[[httpx.Response], SpotifyError],
    credentials: Callable[[], tuple[str, ...]],
) -> httpx.Response:
    """Move playlist entries, retrying only explicit rate-limit rejections."""
    return _request_playlist_write(request, context, error_from_response, credentials, 200)


def request_append(
    request: Callable[[], httpx.Response],
    context: str,
    error_from_response: Callable[[httpx.Response], SpotifyError],
    credentials: Callable[[], tuple[str, ...]],
) -> httpx.Response:
    """Append playlist entries, retrying only explicit rate-limit rejections."""
    return _request_playlist_write(request, context, error_from_response, credentials, 201)


def _request_playlist_write(
    request: Callable[[], httpx.Response],
    context: str,
    error_from_response: Callable[[httpx.Response], SpotifyError],
    credentials: Callable[[], tuple[str, ...]],
    expected_status: int,
) -> httpx.Response:
    """Require the documented write response and never replay an uncertain mutation."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = request()
        except httpx.TransportError as error:
            raise SpotifyOutcomeUncertainError(
                f"{context} has an uncertain outcome after {type(error).__name__}: "
                f"{redact(str(error), credentials())}. "
                "Spotify may have applied the write. Inspect the playlist and rerun "
                "the command to read its current order before applying further changes; "
                "the write was not retried."
            ) from None
        if response.status_code == expected_status:
            return response
        if 200 <= response.status_code <= 299 or 500 <= response.status_code <= 599:
            raise SpotifyOutcomeUncertainError(
                f"{error_from_response(response)}; expected HTTP {expected_status}. "
                "Spotify may have applied the write. "
                "Inspect the playlist and rerun the command to read its current order before "
                "applying further "
                "changes; the write was not retried."
            )
        if response.status_code != 429:
            raise error_from_response(response)
        delay = _rate_limit_delay(response, error_from_response)
        if attempt == MAX_ATTEMPTS:
            raise error_from_response(response)
        _wait_for_retry(context, attempt, delay, "HTTP 429")
    raise AssertionError("Retry loop exhausted without a response or explicit error")
