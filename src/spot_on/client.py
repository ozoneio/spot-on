"""Direct Spotify Web API connector for playlist inspection and safe writes."""

import logging
import re
import time
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spot_on.errors import ConfigurationError, SpotifyError, SpotifyOutcomeUncertainError
from spot_on.models import (
    PlaylistEntry,
    PlaylistMetadata,
    PlaylistPage,
    PlaylistSnapshot,
    ReorderRequest,
)
from spot_on.oauth import parse_token, request_token
from spot_on.transport import (
    redact,
    request_append,
    request_reorder,
    request_with_retries,
    response_error,
)

API_URL = "https://api.spotify.com/v1"
PAGE_LIMIT = 50
logger = logging.getLogger(__name__)
CatalogTrackUri = Annotated[str, Field(pattern=r"^spotify:track:[A-Za-z0-9]{22}$")]


class AppendRequest(BaseModel):
    """Append one documented batch of catalog tracks without replacing any entries."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    uris: Annotated[tuple[CatalogTrackUri, ...], Field(min_length=1, max_length=100)]


def _playlist_url(playlist_id: str) -> str:
    """Restrict IDs to Spotify's base62 alphabet before constructing API paths."""
    if re.fullmatch(r"[A-Za-z0-9]+", playlist_id) is None:
        raise ConfigurationError("Spotify playlist ID must be a nonempty base62 identifier.")
    return f"{API_URL}/playlists/{playlist_id}"


def _parse_response[Model: BaseModel](
    response: httpx.Response, model: type[Model], context: str, credentials: tuple[str, ...]
) -> Model:
    """Validate consumed response fields with actionable, credential-safe details."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError as error:
        raise SpotifyError(
            f"{context} returned invalid data; HTTP {response.status_code}; validation: "
            f"{error.json(include_input=False, include_url=False)}; "
            f"response body: {redact(response.text, credentials)}"
        ) from None


def _validate_next_page(next_url: str, endpoint: str, next_offset: int) -> None:
    """Validate pagination links without sending credentials to server-provided URLs."""
    try:
        next_page = urlsplit(next_url)
    except ValueError as error:
        raise SpotifyError(f"GET {endpoint} returned an invalid next-page URL.") from error
    expected = urlsplit(endpoint)
    if (
        next_page.scheme != "https"
        or next_page.netloc != "api.spotify.com"
        or next_page.path != expected.path
        or next_page.fragment
    ):
        raise SpotifyError(f"GET {endpoint} returned an untrusted next-page URL.")
    query = parse_qs(next_page.query, keep_blank_values=True)
    if query.get("offset") != [str(next_offset)] or query.get("limit") != [str(PAGE_LIMIT)]:
        raise SpotifyError(
            f"GET {endpoint} returned inconsistent next-page parameters; "
            f"expected offset={next_offset}, limit={PAGE_LIMIT}."
        )


def _write_snapshot(response: httpx.Response, context: str, credentials: tuple[str, ...]) -> str:
    """Treat an accepted write without a valid snapshot as an uncertain mutation."""
    try:
        result = _parse_response(response, PlaylistSnapshot, context, credentials)
    except SpotifyError as error:
        raise SpotifyOutcomeUncertainError(
            f"{error}. Spotify accepted the write but its resulting snapshot could not "
            "be confirmed. Inspect the playlist before applying further changes."
        ) from error
    return result.snapshot_id


class SpotifyClient:
    """Refresh access credentials and call fixed Spotify endpoints via an owned HTTP client."""

    def __init__(
        self, http: httpx.Client, client_id: str, client_secret: str, refresh_token: str
    ) -> None:
        if not refresh_token or refresh_token != refresh_token.strip():
            raise ConfigurationError("SPOTIFY_REFRESH_TOKEN must be nonempty without whitespace.")
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = ""
        self._expires_at = 0.0

    def _headers(self) -> dict[str, str]:
        """Refresh access tokens before expiry; retain documented optional token rotations."""
        if time.monotonic() >= self._expires_at:
            response = request_token(
                self._http,
                self._client_id,
                self._client_secret,
                {"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            )
            token = parse_token(response)
            self._access_token = token.access_token
            self._expires_at = time.monotonic() + token.expires_in * 0.9
            if token.refresh_token is not None and token.refresh_token != self._refresh_token:
                self._refresh_token = token.refresh_token
                logger.warning(
                    "Spotify rotated the refresh token; authorize again and update "
                    "SPOTIFY_REFRESH_TOKEN before the next run",
                    extra={"operation": "refresh_token", "refresh_token_rotated": True},
                )
        return {"Authorization": f"Bearer {self._access_token}"}

    def _credentials(self) -> tuple[str, ...]:
        """Collect credentials used solely to redact response failures."""
        return (self._client_secret, self._refresh_token, self._access_token)

    def _get(self, endpoint: str, params: dict[str, str | int]) -> httpx.Response:
        """Fetch a Spotify resource with bounded transient-error retries."""
        context = f"GET {endpoint} (params={params})"
        return request_with_retries(
            lambda: self._http.get(
                endpoint, headers=self._headers(), params=params, follow_redirects=False
            ),
            context,
            lambda response: response_error(response, context, self._credentials()),
            self._credentials,
        )

    def playlist(self, playlist_id: str) -> PlaylistMetadata:
        """Fetch identity and snapshot without loading embedded playlist contents."""
        endpoint = _playlist_url(playlist_id)
        response = self._get(endpoint, {"fields": "id,name,snapshot_id"})
        playlist = _parse_response(
            response, PlaylistMetadata, f"GET {endpoint}", self._credentials()
        )
        if playlist.id != playlist_id:
            raise SpotifyError(f"GET {endpoint} returned a different playlist ID: {playlist.id}.")
        return playlist

    def playlist_entries(self, playlist_id: str) -> tuple[PlaylistEntry, ...]:
        """Read every original position, retaining local, episode, and unavailable entries."""
        endpoint = f"{_playlist_url(playlist_id)}/items"
        entries: list[PlaylistEntry] = []
        offset = 0
        total: int | None = None
        while True:
            response = self._get(
                endpoint,
                {"limit": PAGE_LIMIT, "offset": offset, "additional_types": "track,episode"},
            )
            page = _parse_response(response, PlaylistPage, f"GET {endpoint}", self._credentials())
            if page.offset != offset or page.limit != PAGE_LIMIT:
                raise SpotifyError(
                    f"GET {endpoint} returned inconsistent pagination; requested "
                    f"offset={offset}, limit={PAGE_LIMIT}; received "
                    f"offset={page.offset}, limit={page.limit}."
                )
            if total is not None and page.total != total:
                raise SpotifyError(
                    f"GET {endpoint} playlist size changed during pagination "
                    f"from {total} to {page.total}; rerun the command to read its current order."
                )
            total = page.total
            expected_count = min(PAGE_LIMIT, total - offset)
            if expected_count < 0 or len(page.items) != expected_count:
                raise SpotifyError(
                    f"GET {endpoint} returned {len(page.items)} items at offset={offset}, "
                    f"but total={total} requires {expected_count}; no entries were discarded."
                )
            entries.extend(page.items)
            offset += len(page.items)
            if page.next is None:
                if offset != total:
                    raise SpotifyError(
                        f"GET {endpoint} ended pagination after {offset} of {total} entries."
                    )
                return tuple(entries)
            if offset >= total:
                raise SpotifyError(
                    f"GET {endpoint} returned a next page after all {total} entries."
                )
            _validate_next_page(page.next, endpoint, offset)

    def reorder(
        self, playlist_id: str, range_start: int, insert_before: int, snapshot_id: str
    ) -> str:
        """Move one entry using a snapshot; never replay a write with an uncertain outcome."""
        endpoint = f"{_playlist_url(playlist_id)}/items"
        try:
            payload = ReorderRequest(
                range_start=range_start,
                insert_before=insert_before,
                range_length=1,
                snapshot_id=snapshot_id,
            )
        except ValidationError as error:
            raise ConfigurationError(
                "Reorder requires nonnegative integer positions and a nonempty snapshot ID; "
                f"validation: {error.json(include_input=False, include_url=False)}"
            ) from error
        context = f"PUT {endpoint} (body={payload.model_dump_json()})"
        response = request_reorder(
            lambda: self._http.put(
                endpoint,
                headers={**self._headers(), "Content-Type": "application/json"},
                content=payload.model_dump_json(),
                follow_redirects=False,
            ),
            context,
            lambda response: response_error(response, context, self._credentials()),
            self._credentials,
        )
        return _write_snapshot(response, context, self._credentials())

    def append_items(self, playlist_id: str, uris: tuple[str, ...]) -> str:
        """Append at most 100 catalog tracks; uncertain outcomes are never replayed."""
        endpoint = f"{_playlist_url(playlist_id)}/items"
        try:
            payload = AppendRequest(uris=uris)
        except ValidationError as error:
            raise ConfigurationError(
                "Append requires 1 to 100 Spotify catalog track URIs with base62 IDs; "
                f"validation: {error.json(include_input=False, include_url=False)}"
            ) from error
        context = f"POST {endpoint} (body={payload.model_dump_json()})"
        response = request_append(
            lambda: self._http.post(
                endpoint,
                headers={**self._headers(), "Content-Type": "application/json"},
                content=payload.model_dump_json(),
                follow_redirects=False,
            ),
            context,
            lambda response: response_error(response, context, self._credentials()),
            self._credentials,
        )
        return _write_snapshot(response, context, self._credentials())
