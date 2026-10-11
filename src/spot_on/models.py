"""Immutable, strictly validated portions of Spotify's API responses."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonemptyString = Annotated[str, Field(min_length=1)]
NonnegativeInteger = Annotated[int, Field(ge=0)]


class SpotifyModel(BaseModel):
    """Require consumed fields and ignore unrelated Spotify metadata."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


class Artist(SpotifyModel):
    """An artist's displayed name."""

    name: str


class Album(SpotifyModel):
    """The album release date used to order a track."""

    name: str
    release_date: str
    release_date_precision: Literal["year", "month", "day"]


class Track(SpotifyModel):
    """A music track; optional duration keeps existing schema-1 backups readable."""

    type: Literal["track"]
    id: str | None
    uri: NonemptyString
    name: str
    artists: tuple[Artist, ...]
    album: Album
    duration_ms: NonnegativeInteger | None = None


class LocalAlbum(SpotifyModel):
    """Local-file album metadata does not include a catalog release date."""

    name: str


class LocalTrack(SpotifyModel):
    """A local file whose identity and metadata remain available in backups."""

    type: Literal["track"]
    id: None
    uri: NonemptyString
    name: str
    artists: tuple[Artist, ...]
    album: LocalAlbum


class Episode(SpotifyModel):
    """A podcast entry retained so playlist operations can reject it explicitly."""

    type: Literal["episode"]
    id: str | None
    uri: NonemptyString
    name: str


PlaylistItem = Track | LocalTrack | Episode


class PlaylistEntry(SpotifyModel):
    """An entry at its original playlist position, including unavailable items."""

    is_local: bool
    item: PlaylistItem | None
    added_at: str | None

    @model_validator(mode="after")
    def _require_local_marker(self) -> Self:
        """Missing catalog release metadata is accepted only for marked local files."""
        if isinstance(self.item, LocalTrack) and not self.is_local:
            raise ValueError(
                "A track without catalog release metadata must have is_local=true; "
                "a catalog track requires album.release_date and album.release_date_precision."
            )
        return self


class PlaylistMetadata(SpotifyModel):
    """Playlist identity and the version used to detect concurrent changes."""

    id: NonemptyString
    name: str
    snapshot_id: NonemptyString


class PlaylistPage(SpotifyModel):
    """One complete page of playlist entries and its pagination metadata."""

    items: tuple[PlaylistEntry, ...]
    offset: NonnegativeInteger
    limit: Annotated[int, Field(ge=1, le=50)]
    total: NonnegativeInteger
    next: str | None


class PlaylistSnapshot(SpotifyModel):
    """The new playlist version returned after a reorder."""

    snapshot_id: NonemptyString


class ReorderRequest(SpotifyModel):
    """Move exactly one entry without replacing playlist contents."""

    range_start: NonnegativeInteger
    insert_before: NonnegativeInteger
    range_length: Literal[1]
    snapshot_id: NonemptyString


class TokenResponse(SpotifyModel):
    """An access token and the optional refresh-token rotation from Spotify."""

    access_token: NonemptyString
    token_type: Literal["Bearer"]
    expires_in: Annotated[int, Field(gt=0)]
    refresh_token: NonemptyString | None = None


class AuthorizationTokenResponse(TokenResponse):
    """Initial authorization must return a refresh token for future runs."""

    refresh_token: NonemptyString


class OAuthErrorResponse(SpotifyModel):
    """OAuth error details safe to display after credential redaction."""

    error: str
    error_description: str | None = None
