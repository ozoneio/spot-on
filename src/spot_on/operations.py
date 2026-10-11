"""Spotify operations that capture a consistent order and preserve track membership."""

from pathlib import Path

from spot_on.backups import PlaylistBackup, write_backup
from spot_on.client import SpotifyClient
from spot_on.errors import PlaylistError
from spot_on.models import PlaylistEntry, PlaylistMetadata
from spot_on.playlists import reorder_moves, restore_positions
from spot_on.sorting import SortKey, SortOrder, sort_positions


def read_playlist(
    client: SpotifyClient, playlist_id: str
) -> tuple[PlaylistMetadata, tuple[PlaylistEntry, ...]]:
    """Reject a playlist edited during pagination, rather than backing up a mixed snapshot."""
    playlist = client.playlist(playlist_id)
    entries = client.playlist_entries(playlist_id)
    assert_snapshot(client, playlist_id, playlist.snapshot_id)
    return playlist, entries


def assert_snapshot(client: SpotifyClient, playlist_id: str, snapshot_id: str) -> None:
    """Detect concurrent playlist edits before making an order-dependent request."""
    current = client.playlist(playlist_id)
    if current.snapshot_id != snapshot_id:
        raise PlaylistError(
            f"Playlist {playlist_id} changed concurrently: expected snapshot {snapshot_id!r}, "
            f"received {current.snapshot_id!r}. Rerun after other playlist edits finish."
        )


def apply_order(
    client: SpotifyClient, playlist_id: str, snapshot_id: str, positions: tuple[int, ...]
) -> None:
    """Apply planned single-entry moves with a current snapshot before each request."""
    moves = reorder_moves(positions)
    current_snapshot = snapshot_id
    for move in moves:
        assert_snapshot(client, playlist_id, current_snapshot)
        current_snapshot = client.reorder(
            playlist_id, move.range_start, move.insert_before, current_snapshot
        )
    assert_snapshot(client, playlist_id, current_snapshot)


def sort_playlist(
    client: SpotifyClient,
    playlist_id: str,
    backup_directory: Path,
    sort_by: SortKey,
    order: SortOrder,
) -> Path:
    """Validate the selected sort, back up the playlist, then reorder existing occurrences."""
    playlist, entries = read_playlist(client, playlist_id)
    positions = sort_positions(entries, sort_by, order)
    path = write_backup(backup_directory, playlist, entries)
    apply_order(client, playlist_id, playlist.snapshot_id, positions)
    return path


def restore_playlist(client: SpotifyClient, backup: PlaylistBackup, backup_directory: Path) -> Path:
    """Back up the current order and restore an earlier order with identical membership."""
    playlist, entries = read_playlist(client, backup.playlist_id)
    positions = restore_positions(entries, backup.entries)
    path = write_backup(backup_directory, playlist, entries)
    apply_order(client, playlist.id, playlist.snapshot_id, positions)
    return path
