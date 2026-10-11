from itertools import permutations
from typing import Literal

import pytest

from spot_on.errors import PlaylistError
from spot_on.models import Album, Artist, Episode, PlaylistEntry, Track
from spot_on.playlists import duplicate_groups, reorder_moves, restore_positions, sorted_positions


def track_entry(
    number: int,
    name: str,
    artists: tuple[str, ...],
    release_date: str,
    precision: Literal["year", "month", "day"],
) -> PlaylistEntry:
    track_id = f"{number:022d}"
    return PlaylistEntry(
        is_local=False,
        item=Track(
            type="track",
            id=track_id,
            uri=f"spotify:track:{track_id}",
            name=name,
            artists=tuple(Artist(name=artist) for artist in artists),
            album=Album(
                name="Album",
                release_date=release_date,
                release_date_precision=precision,
            ),
        ),
        added_at=None,
    )


def test_duplicate_groups_preserve_exact_title_artist_order_and_occurrences() -> None:
    entries = (
        track_entry(1, "Same Song", ("First", "Second"), "2020", "year"),
        track_entry(2, "Same Song", ("Second", "First"), "2020", "year"),
        track_entry(3, "Same Song", ("First", "Second"), "2024", "year"),
        track_entry(4, "same song", ("First", "Second"), "2020", "year"),
        track_entry(1, "Same Song", ("First", "Second"), "2020", "year"),
    )

    groups = duplicate_groups(entries)

    assert len(groups) == 1
    assert groups[0].name == "Same Song"
    assert groups[0].artists == ("First", "Second")
    assert groups[0].positions == (0, 2, 4)
    assert groups[0].uris == (
        "spotify:track:0000000000000000000001",
        "spotify:track:0000000000000000000003",
        "spotify:track:0000000000000000000001",
    )
    assert entries[0] == entries[4]


def test_release_sort_normalizes_partial_dates_and_preserves_ties() -> None:
    entries = (
        track_entry(1, "Year", ("Artist",), "2023", "year"),
        track_entry(2, "Latest", ("Artist",), "2024-02-29", "day"),
        track_entry(3, "January", ("Artist",), "2023-01", "month"),
        track_entry(4, "March", ("Artist",), "2023-03", "month"),
        track_entry(5, "First day", ("Artist",), "2023-01-01", "day"),
        track_entry(4, "March", ("Artist",), "2023-03", "month"),
    )

    assert sorted_positions(entries) == (1, 3, 5, 0, 2, 4)
    assert tuple(entry.item.name for entry in entries if entry.item) == (
        "Year",
        "Latest",
        "January",
        "March",
        "First day",
        "March",
    )


@pytest.mark.parametrize(
    "invalid_entry",
    (
        PlaylistEntry(is_local=False, item=None, added_at=None),
        PlaylistEntry(
            is_local=True,
            item=Track(
                type="track",
                id=None,
                uri="spotify:local:artist:album:song:1",
                name="Local song",
                artists=(Artist(name="Artist"),),
                album=Album(name="Album", release_date="2020", release_date_precision="year"),
            ),
            added_at=None,
        ),
        PlaylistEntry(
            is_local=False,
            item=Episode(
                type="episode",
                id="0000000000000000000001",
                uri="spotify:episode:0000000000000000000001",
                name="Episode",
            ),
            added_at=None,
        ),
    ),
)
def test_playlist_operations_reject_unsupported_entries(
    invalid_entry: PlaylistEntry,
) -> None:
    entries = (
        track_entry(1, "Song", ("Artist",), "2020", "year"),
        invalid_entry,
    )

    with pytest.raises(PlaylistError):
        duplicate_groups(entries)
    with pytest.raises(PlaylistError):
        sorted_positions(entries)


def test_reorder_moves_reproduce_every_small_playlist_permutation() -> None:
    for size in range(7):
        for target in permutations(range(size)):
            current = list(range(size))
            for move in reorder_moves(target):
                assert 0 <= move.insert_before < move.range_start < size
                entry = current.pop(move.range_start)
                current.insert(move.insert_before, entry)
            assert tuple(current) == target


@pytest.mark.parametrize("target", ((-1,), (1,), (0, 0), (0, 2), (0, 1, 3)))
def test_reorder_moves_reject_invalid_permutations(target: tuple[int, ...]) -> None:
    with pytest.raises(PlaylistError):
        reorder_moves(target)


def test_restore_positions_match_repeated_uri_occurrences_stably() -> None:
    first = track_entry(1, "First", ("Artist",), "2020", "year")
    second = track_entry(2, "Second", ("Artist",), "2020", "year")
    third = track_entry(3, "Third", ("Artist",), "2020", "year")
    current = (second, first, third, first)
    backup = (first, second, first, third)

    assert restore_positions(current, backup) == (1, 0, 3, 2)


def test_restore_positions_reject_changed_membership_and_duplicate_counts() -> None:
    first = track_entry(1, "First", ("Artist",), "2020", "year")
    second = track_entry(2, "Second", ("Artist",), "2020", "year")
    backup = (first, first, second)

    for current in ((first, second), (first, second, second)):
        with pytest.raises(PlaylistError, match="membership differs"):
            restore_positions(current, backup)


@pytest.mark.parametrize(
    ("release_date", "precision"),
    (("2023-02-29", "day"), ("2023-13", "month"), ("2023-01", "year")),
)
def test_sort_rejects_invalid_dates_or_mismatched_precision(
    release_date: str, precision: Literal["year", "month", "day"]
) -> None:
    entry = track_entry(1, "Song", ("Artist",), release_date, precision)

    with pytest.raises(PlaylistError):
        sorted_positions((entry,))
