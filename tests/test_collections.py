import pytest
from test_playlists import track_entry

from spot_on.collections import compare_entries, merge_uris
from spot_on.errors import PlaylistError
from spot_on.models import Episode, PlaylistEntry, Track
from spot_on.playlists import require_tracks


def test_comparison_retains_exact_identities_counts_and_source_order() -> None:
    first = track_entry(1, "Same song", ("Artist",), "2020", "year")
    alternate = track_entry(2, "Same song", ("Artist",), "2020", "year")
    shared = track_entry(3, "Shared", ("First", "Second"), "2020", "year")
    right_only = track_entry(4, "Other", ("Artist",), "2020", "year")
    left = (shared, first, alternate, first, shared)
    right = (right_only, first, first, shared)

    comparison = compare_entries("left", left, "right", right)

    assert comparison.left_playlist_id == "left"
    assert comparison.right_playlist_id == "right"
    assert tuple(track.name for track in comparison.common) == ("Shared", "Same song")
    assert comparison.common[0].artists == ("First", "Second")
    assert comparison.common[0].left_positions == (0, 4)
    assert comparison.common[0].right_positions == (3,)
    assert comparison.common[1].left_positions == (1, 3)
    assert comparison.common[1].right_positions == (1, 2)
    assert len(comparison.only_left) == 1
    assert comparison.only_left[0].uri == require_tracks((alternate,))[0].uri
    assert comparison.only_left[0].left_positions == (2,)
    assert comparison.only_left[0].right_positions == ()
    assert len(comparison.only_right) == 1
    assert comparison.only_right[0].left_positions == ()
    assert comparison.only_right[0].right_positions == (0,)
    assert left == (shared, first, alternate, first, shared)


def test_comparison_handles_empty_and_identical_playlists() -> None:
    first = track_entry(1, "Song", ("Artist",), "2020", "year")
    assert compare_entries("left", (), "right", ()).common == ()
    empty_left = compare_entries("left", (), "right", (first, first))
    assert empty_left.common == empty_left.only_left == ()
    assert empty_left.only_right[0].right_positions == (0, 1)
    same = compare_entries("same", (first, first), "same", (first, first))
    assert same.only_left == same.only_right == ()
    assert same.common[0].left_positions == same.common[0].right_positions == (0, 1)


def test_merge_appends_missing_uris_once_in_source_order_and_is_idempotent() -> None:
    first = track_entry(1, "Song", ("Artist",), "2020", "year")
    second = track_entry(2, "Song", ("Artist",), "2020", "year")
    third = track_entry(3, "Third", ("Artist",), "2020", "year")
    fourth = track_entry(4, "Fourth", ("Artist",), "2020", "year")
    destination = (first, first)
    sources = ((first, third, third, second), (second, fourth, third))

    additions = merge_uris(destination, sources)

    assert additions == tuple(track.uri for track in require_tracks((third, second, fourth)))
    assert merge_uris(destination + (third, second, fourth), sources) == ()
    assert destination == (first, first)
    assert sources == ((first, third, third, second), (second, fourth, third))
    assert merge_uris((), ((), ())) == ()


@pytest.mark.parametrize(
    "unsupported",
    (
        PlaylistEntry(is_local=False, item=None, added_at=None),
        PlaylistEntry(is_local=True, item=None, added_at=None),
        PlaylistEntry(
            is_local=False,
            item=Episode(type="episode", id="episode", uri="spotify:episode:episode", name="Show"),
            added_at=None,
        ),
    ),
)
def test_compare_and_merge_refuse_unsupported_entries_in_either_input(
    unsupported: PlaylistEntry,
) -> None:
    first = track_entry(1, "Song", ("Artist",), "2020", "year")
    for left, right in (((unsupported,), (first,)), ((first,), (unsupported,))):
        with pytest.raises(PlaylistError):
            compare_entries("left", left, "right", right)
        with pytest.raises(PlaylistError):
            merge_uris(left, (right,))


def test_merge_validates_later_source_uris_before_producing_any_additions() -> None:
    first = track_entry(1, "Song", ("Artist",), "2020", "year")
    track = require_tracks((first,))[0]
    malformed = PlaylistEntry(
        is_local=False,
        item=Track(
            type="track",
            id="invalid-ID",
            uri="spotify:track:invalid-ID",
            name=track.name,
            artists=track.artists,
            album=track.album,
        ),
        added_at=None,
    )
    with pytest.raises(PlaylistError, match="base62"):
        merge_uris((), ((first,), (malformed,)))
