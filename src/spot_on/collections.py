"""Compare exact catalog identities and merge tracks without replacing existing entries."""

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from spot_on.backups import write_backup
from spot_on.client import CatalogTrackUri, SpotifyClient
from spot_on.errors import PlaylistError, SpotifyError
from spot_on.models import PlaylistEntry, Track
from spot_on.operations import assert_snapshot, read_playlist
from spot_on.playlists import require_tracks


class TrackComparison(BaseModel):
    """One exact track identity with every zero-based occurrence in both playlists."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    uri: str
    name: str
    artists: tuple[str, ...]
    left_positions: tuple[int, ...]
    right_positions: tuple[int, ...]


class PlaylistComparison(BaseModel):
    """Common identities follow left order; each exclusive group follows its source order."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    left_playlist_id: str
    right_playlist_id: str
    common: tuple[TrackComparison, ...]
    only_left: tuple[TrackComparison, ...]
    only_right: tuple[TrackComparison, ...]


class MergeResult(BaseModel):
    """Confirmed additions and a backup that records prior membership and order.

    Existing order restoration cannot remove merged tracks. The backup is a record for
    manual recovery; reversing a merge requires removing its additions in Spotify.
    """

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    playlist_id: str
    source_playlist_ids: tuple[str, ...]
    added_count: int
    backup_file: str


def _track_positions(tracks: tuple[Track, ...]) -> dict[str, tuple[int, ...]]:
    """Collect occurrence positions while retaining each identity's first appearance."""
    positions: dict[str, list[int]] = defaultdict(list)
    for position, track in enumerate(tracks):
        positions[track.uri].append(position)
    return {uri: tuple(indices) for uri, indices in positions.items()}


def _comparison(
    track: Track, left_positions: tuple[int, ...], right_positions: tuple[int, ...]
) -> TrackComparison:
    """Retain display metadata alongside exact identity and occurrence counts."""
    return TrackComparison(
        uri=track.uri,
        name=track.name,
        artists=tuple(artist.name for artist in track.artists),
        left_positions=left_positions,
        right_positions=right_positions,
    )


def compare_entries(
    left_id: str,
    left_entries: tuple[PlaylistEntry, ...],
    right_id: str,
    right_entries: tuple[PlaylistEntry, ...],
) -> PlaylistComparison:
    """Compare exact URIs, retaining duplicates rather than equating alternate recordings."""
    left_tracks = require_tracks(left_entries)
    right_tracks = require_tracks(right_entries)
    left_positions = _track_positions(left_tracks)
    right_positions = _track_positions(right_tracks)
    return PlaylistComparison(
        left_playlist_id=left_id,
        right_playlist_id=right_id,
        common=tuple(
            _comparison(left_tracks[positions[0]], positions, right_positions[uri])
            for uri, positions in left_positions.items()
            if uri in right_positions
        ),
        only_left=tuple(
            _comparison(left_tracks[positions[0]], positions, ())
            for uri, positions in left_positions.items()
            if uri not in right_positions
        ),
        only_right=tuple(
            _comparison(right_tracks[positions[0]], (), positions)
            for uri, positions in right_positions.items()
            if uri not in left_positions
        ),
    )


def compare_playlists(
    client: SpotifyClient, left_playlist_id: str, right_playlist_id: str
) -> PlaylistComparison:
    """Report two consistent snapshots, rejecting edits during either playlist read."""
    left_playlist, left_entries = read_playlist(client, left_playlist_id)
    right_playlist, right_entries = read_playlist(client, right_playlist_id)
    result = compare_entries(left_playlist_id, left_entries, right_playlist_id, right_entries)
    assert_snapshot(client, left_playlist_id, left_playlist.snapshot_id)
    assert_snapshot(client, right_playlist_id, right_playlist.snapshot_id)
    return result


def _catalog_uris(entries: tuple[PlaylistEntry, ...]) -> tuple[str, ...]:
    """Validate every track URI before any source can contribute an append batch."""
    uris = tuple(track.uri for track in require_tracks(entries))
    try:
        return TypeAdapter(tuple[CatalogTrackUri, ...]).validate_python(uris, strict=True)
    except ValidationError as error:
        raise PlaylistError(
            "Playlist entries require Spotify catalog track URIs with 22-character base62 IDs; "
            f"validation: {error.json(include_input=False, include_url=False)}"
        ) from error


def merge_uris(
    destination_entries: tuple[PlaylistEntry, ...],
    source_entries: tuple[tuple[PlaylistEntry, ...], ...],
) -> tuple[str, ...]:
    """Return missing identities once in source order, preserving destination duplicates."""
    destination_uris = _catalog_uris(destination_entries)
    sources = tuple(_catalog_uris(entries) for entries in source_entries)
    seen = set(destination_uris)
    additions: list[str] = []
    for source in sources:
        for uri in source:
            if uri not in seen:
                additions.append(uri)
                seen.add(uri)
    return tuple(additions)


def merge_playlists(
    client: SpotifyClient,
    playlist_id: str,
    source_playlist_ids: tuple[str, ...],
    backup_directory: Path,
) -> MergeResult:
    """Back up and append missing identities in batches, verifying every resulting order.

    A failure retains already applied batches and reports the confirmed addition count.
    Inspect uncertain writes before rerunning; the order-only restore command cannot undo
    added membership. Spotify's append endpoint has no atomic snapshot precondition, so a
    concurrent edit between the preflight check and POST is detected by the subsequent read.
    """
    if not source_playlist_ids:
        raise PlaylistError(f"Merge into playlist {playlist_id} requires at least one source.")
    if len(set(source_playlist_ids)) != len(source_playlist_ids):
        raise PlaylistError(
            f"Merge into playlist {playlist_id} has repeated sources: {source_playlist_ids!r}."
        )
    if playlist_id in source_playlist_ids:
        raise PlaylistError(f"Merge destination {playlist_id} cannot also be a source playlist.")
    playlist, entries = read_playlist(client, playlist_id)
    sources = tuple(read_playlist(client, source_id) for source_id in source_playlist_ids)
    additions = merge_uris(entries, tuple(source_entries for _, source_entries in sources))
    path = write_backup(backup_directory, playlist, entries)
    confirmed_added = 0
    expected_uris = _catalog_uris(entries)
    current_snapshot = playlist.snapshot_id
    try:
        for source_playlist, _ in sources:
            assert_snapshot(client, source_playlist.id, source_playlist.snapshot_id)
        assert_snapshot(client, playlist_id, current_snapshot)
        for offset in range(0, len(additions), 100):
            batch = additions[offset : offset + 100]
            for source_playlist, _ in sources:
                assert_snapshot(client, source_playlist.id, source_playlist.snapshot_id)
            assert_snapshot(client, playlist_id, current_snapshot)
            resulting_snapshot = client.append_items(playlist_id, batch)
            expected_uris += batch
            updated_playlist, updated_entries = read_playlist(client, playlist_id)
            if (
                updated_playlist.snapshot_id != resulting_snapshot
                or _catalog_uris(updated_entries) != expected_uris
            ):
                raise PlaylistError(
                    f"Playlist {playlist_id} changed during merge verification: expected "
                    f"snapshot {resulting_snapshot!r} and {len(expected_uris)} entries in the "
                    f"planned order; received snapshot {updated_playlist.snapshot_id!r} and "
                    f"{len(updated_entries)} entries. Inspect the playlist before rerunning."
                )
            confirmed_added += len(batch)
            current_snapshot = resulting_snapshot
        assert_snapshot(client, playlist_id, current_snapshot)
    except (PlaylistError, SpotifyError) as error:
        raise type(error)(
            f"Merge into playlist {playlist_id} stopped after {confirmed_added} confirmed "
            f"additions. Earlier batches remain; the last write may also have applied. "
            f"Backup: {path}. Inspect current membership before rerunning; order restoration "
            f"cannot remove additions. Cause: {error}"
        ) from error
    return MergeResult(
        playlist_id=playlist_id,
        source_playlist_ids=source_playlist_ids,
        added_count=confirmed_added,
        backup_file=str(path),
    )
