"""Pure playlist analysis and ordering; positions distinguish repeated track occurrences."""

import re
from collections import Counter, defaultdict, deque
from datetime import date
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict

from spot_on.errors import PlaylistError
from spot_on.models import Album, PlaylistEntry, Track


class DuplicateGroup(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    name: str
    artists: tuple[str, ...]
    positions: tuple[int, ...]
    uris: tuple[str, ...]


class ReorderMove(NamedTuple):
    range_start: int
    insert_before: int


def require_tracks(entries: tuple[PlaylistEntry, ...]) -> tuple[Track, ...]:
    """Reject entries that catalog-track operations cannot handle without losing data."""
    tracks: list[Track] = []
    for position, entry in enumerate(entries):
        if entry.is_local:
            raise PlaylistError(
                f"Entry at position {position} is a local file. "
                "Use a playlist containing only Spotify catalog tracks."
            )
        if not isinstance(entry.item, Track) or entry.item.id is None:
            raise PlaylistError(
                f"Entry at position {position} is unavailable or is not a catalog track. "
                "Use a playlist containing only Spotify catalog tracks."
            )
        if entry.item.uri != f"spotify:track:{entry.item.id}":
            raise PlaylistError(
                f"Entry at position {position} has an inconsistent track ID and URI: "
                f"id={entry.item.id!r}, uri={entry.item.uri!r}."
            )
        tracks.append(entry.item)
    return tuple(tracks)


def duplicate_groups(entries: tuple[PlaylistEntry, ...]) -> tuple[DuplicateGroup, ...]:
    """Report candidates sharing the exact title and ordered artist names, without removing any."""
    tracks = require_tracks(entries)
    positions: dict[tuple[str, tuple[str, ...]], list[int]] = defaultdict(list)
    for position, track in enumerate(tracks):
        key = (track.name, tuple(artist.name for artist in track.artists))
        positions[key].append(position)
    return tuple(
        DuplicateGroup(
            name=name,
            artists=artists,
            positions=tuple(indices),
            uris=tuple(tracks[index].uri for index in indices),
        )
        for (name, artists), indices in positions.items()
        if len(indices) > 1
    )


def release_date(album: Album) -> date:
    """Interpret year/month precision as the first day of that period; reject invalid dates."""
    patterns = {
        "year": r"[0-9]{4}",
        "month": r"[0-9]{4}-[0-9]{2}",
        "day": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
    }
    if re.fullmatch(patterns[album.release_date_precision], album.release_date) is None:
        raise PlaylistError(
            f"Album {album.name!r} has release_date={album.release_date!r} inconsistent "
            f"with release_date_precision={album.release_date_precision!r}."
        )
    components = tuple(int(part) for part in album.release_date.split("-"))
    year = components[0]
    month = components[1] if len(components) >= 2 else 1
    day = components[2] if len(components) == 3 else 1
    try:
        return date(year, month, day)
    except ValueError as error:
        raise PlaylistError(
            f"Album {album.name!r} has invalid release_date={album.release_date!r}: {error}."
        ) from error


def sorted_positions(entries: tuple[PlaylistEntry, ...]) -> tuple[int, ...]:
    """Return a stable newest-first permutation using each track's album release date."""
    tracks = require_tracks(entries)
    dates = tuple(release_date(track.album) for track in tracks)
    return tuple(sorted(range(len(tracks)), key=dates.__getitem__, reverse=True))


def reorder_moves(target_positions: tuple[int, ...]) -> tuple[ReorderMove, ...]:
    """Plan moves from later to earlier positions, preserving every original occurrence."""
    current = list(range(len(target_positions)))
    if sorted(target_positions) != current:
        raise PlaylistError(f"Target positions must be a permutation of {current!r}.")
    moves: list[ReorderMove] = []
    for destination, original in enumerate(target_positions):
        source = current.index(original, destination)
        if source != destination:
            moves.append(ReorderMove(range_start=source, insert_before=destination))
            current.insert(destination, current.pop(source))
    return tuple(moves)


def restore_positions(
    entries: tuple[PlaylistEntry, ...], backup_entries: tuple[PlaylistEntry, ...]
) -> tuple[int, ...]:
    """Restore only order; reject changed membership instead of replacing playlist contents."""
    current = require_tracks(entries)
    saved = require_tracks(backup_entries)
    current_counts = Counter(track.uri for track in current)
    saved_counts = Counter(track.uri for track in saved)
    if current_counts != saved_counts:
        raise PlaylistError(
            "Cannot restore order because playlist membership differs from the backup. "
            f"Missing occurrences: {dict(saved_counts - current_counts)!r}; "
            f"extra occurrences: {dict(current_counts - saved_counts)!r}. "
            "Restore the same track membership in Spotify before retrying."
        )
    positions: dict[str, deque[int]] = defaultdict(deque)
    for position, track in enumerate(current):
        positions[track.uri].append(position)
    return tuple(positions[track.uri].popleft() for track in saved)
