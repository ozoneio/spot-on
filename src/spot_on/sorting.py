"""Stable, pure playlist sorting using validated Spotify track metadata."""

import re
from datetime import UTC, date, datetime
from typing import Literal

from spot_on.errors import PlaylistError
from spot_on.models import PlaylistEntry, Track
from spot_on.playlists import release_date, require_tracks

SortKey = Literal["release-date", "title", "artist", "album", "duration", "added-at"]
SortOrder = Literal["asc", "desc"]


def default_sort_order(sort_by: SortKey) -> SortOrder:
    """Order dates newest first and names and durations ascending by default."""
    return "desc" if sort_by in ("release-date", "added-at") else "asc"


def _duration(track: Track, position: int) -> int:
    """Require duration metadata before planning an ordering operation."""
    if track.duration_ms is None:
        raise PlaylistError(
            f"Entry at position {position} has no duration_ms. "
            "Read the playlist again from Spotify before sorting by duration."
        )
    return track.duration_ms


def _added_at(entry: PlaylistEntry, position: int) -> datetime:
    """Require a timezone-aware ISO timestamp and compare the corresponding UTC instant."""
    timestamp = entry.added_at
    pattern = (
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
        r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
    )
    if timestamp is None or re.fullmatch(pattern, timestamp) is None:
        raise PlaylistError(
            f"Entry at position {position} has invalid added_at={timestamp!r}. "
            "Sorting by date added requires a timezone-aware ISO timestamp, "
            "such as 2024-01-01T12:00:00Z."
        )
    try:
        return datetime.fromisoformat(timestamp).astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise PlaylistError(
            f"Entry at position {position} has invalid added_at={timestamp!r}: {error}."
        ) from error


def sort_positions(
    entries: tuple[PlaylistEntry, ...], sort_by: SortKey, order: SortOrder
) -> tuple[int, ...]:
    """Return a stable permutation; album sorting retains the current order within each name."""
    tracks = require_tracks(entries)
    values: tuple[date | datetime | str | tuple[str, ...] | int, ...]
    match sort_by:
        case "release-date":
            values = tuple(release_date(track.album) for track in tracks)
        case "title":
            values = tuple(track.name.casefold() for track in tracks)
        case "artist":
            values = tuple(
                tuple(artist.name.casefold() for artist in track.artists) for track in tracks
            )
        case "album":
            values = tuple(track.album.name.casefold() for track in tracks)
        case "duration":
            values = tuple(_duration(track, position) for position, track in enumerate(tracks))
        case "added-at":
            values = tuple(_added_at(entry, position) for position, entry in enumerate(entries))
    return tuple(sorted(range(len(entries)), key=values.__getitem__, reverse=order == "desc"))
