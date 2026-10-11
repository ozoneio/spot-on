from typing import Literal

import pytest
from pydantic import ValidationError

from spot_on.errors import PlaylistError
from spot_on.models import Album, Artist, Episode, PlaylistEntry, Track
from spot_on.sorting import SortKey, SortOrder, sort_positions


def entry(
    number: int,
    title: str,
    artists: tuple[str, ...],
    album: str,
    release_date: str,
    precision: Literal["year", "month", "day"],
    duration_ms: int | None,
    added_at: str | None,
) -> PlaylistEntry:
    track_id = f"{number:022d}"
    return PlaylistEntry(
        is_local=False,
        item=Track(
            type="track",
            id=track_id,
            uri=f"spotify:track:{track_id}",
            name=title,
            artists=tuple(Artist(name=artist) for artist in artists),
            album=Album(name=album, release_date=release_date, release_date_precision=precision),
            duration_ms=duration_ms,
        ),
        added_at=added_at,
    )


@pytest.mark.parametrize(
    ("sort_by", "order", "expected"),
    (
        ("release-date", "asc", (0, 2, 4, 3, 1)),
        ("release-date", "desc", (1, 3, 0, 2, 4)),
        ("title", "asc", (1, 3, 0, 2, 4)),
        ("title", "desc", (0, 2, 4, 3, 1)),
        ("artist", "asc", (1, 4, 0, 2, 3)),
        ("artist", "desc", (3, 0, 2, 4, 1)),
        ("album", "asc", (3, 1, 2, 0, 4)),
        ("album", "desc", (0, 4, 1, 2, 3)),
        ("duration", "asc", (4, 1, 0, 2, 3)),
        ("duration", "desc", (3, 0, 2, 1, 4)),
        ("added-at", "asc", (0, 2, 3, 4, 1)),
        ("added-at", "desc", (1, 4, 0, 2, 3)),
    ),
)
def test_sorting_preserves_occurrences_and_ties_without_changing_entries(
    sort_by: SortKey, order: SortOrder, expected: tuple[int, ...]
) -> None:
    entries = (
        entry(
            1, "Straße", ("Beta", "Alpha"), "Zulu", "2023", "year", 200_000, "2024-01-01T00:00:00Z"
        ),
        entry(
            2,
            "alpha",
            ("alpha",),
            "Echo",
            "2024-02-29",
            "day",
            100_000,
            "2024-01-02T00:00:00+01:00",
        ),
        entry(
            3,
            "STRASSE",
            ("beta", "alpha"),
            "echo",
            "2023-01",
            "month",
            200_000,
            "2024-01-01T00:00:00Z",
        ),
        entry(
            4,
            "Delta",
            ("Beta", "Beta"),
            "Alpha",
            "2023-03",
            "month",
            300_000,
            "2023-12-31T19:00:00-05:00",
        ),
        entry(
            1, "Straße", ("Beta",), "Zulu", "2023-01-01", "day", 0, "2024-01-01T00:00:00.000001Z"
        ),
    )
    original = tuple(item.model_dump_json() for item in entries)

    assert sort_positions(entries, sort_by, order) == expected
    assert tuple(item.model_dump_json() for item in entries) == original


@pytest.mark.parametrize(
    "added_at",
    (
        None,
        "",
        "2024-01-01",
        "2024-01-01T00:00:00",
        "2024-02-30T00:00:00Z",
        "2024-01-01T00:00:00+00:99",
        "2024-01-01 00:00:00Z",
        "not-a-timestamp",
    ),
)
def test_added_at_sort_rejects_missing_naive_or_malformed_timestamps(added_at: str | None) -> None:
    first = entry(1, "First", ("Artist",), "Album", "2020", "year", 1, "2024-01-01T00:00:00Z")
    invalid = entry(2, "Second", ("Artist",), "Album", "2020", "year", 1, added_at)

    with pytest.raises(PlaylistError, match="position 1.*added_at"):
        sort_positions((first, invalid), "added-at", "desc")


def test_duration_sort_requires_metadata_while_other_sorts_accept_existing_backups() -> None:
    saved = entry(1, "Saved", ("Artist",), "Album", "2020", "year", None, None)
    assert isinstance(saved.item, Track)
    legacy_track = Track.model_validate_json(saved.item.model_dump_json(exclude={"duration_ms"}))

    assert legacy_track.duration_ms is None
    assert sort_positions((saved,), "title", "asc") == (0,)
    with pytest.raises(PlaylistError, match="position 0.*duration_ms"):
        sort_positions((saved,), "duration", "asc")
    with pytest.raises(ValidationError, match="duration_ms"):
        entry(2, "Invalid", ("Artist",), "Album", "2020", "year", -1, None)


@pytest.mark.parametrize(
    "unsupported",
    (
        PlaylistEntry(is_local=False, item=None, added_at=None),
        PlaylistEntry(
            is_local=False,
            item=Episode(type="episode", id="1", uri="spotify:episode:1", name="Episode"),
            added_at=None,
        ),
        entry(1, "Local", ("Artist",), "Album", "2020", "year", 1, None).model_copy(
            update={"is_local": True}
        ),
    ),
)
def test_sorting_rejects_unsupported_items(unsupported: PlaylistEntry) -> None:
    with pytest.raises(PlaylistError, match="position 0"):
        sort_positions((unsupported,), "title", "asc")


def test_release_sort_rejects_invalid_dates_before_returning_a_permutation() -> None:
    invalid = entry(1, "Invalid", ("Artist",), "Album", "2023-02-29", "day", 1, None)

    with pytest.raises(PlaylistError, match="invalid release_date"):
        sort_positions((invalid,), "release-date", "asc")
