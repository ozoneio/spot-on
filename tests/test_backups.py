from datetime import UTC, datetime
from pathlib import Path
from re import escape

import pytest
from pydantic import ValidationError

from spot_on.backups import read_backup, write_backup
from spot_on.errors import BackupError, PlaylistError
from spot_on.models import Episode, LocalTrack, PlaylistEntry, PlaylistMetadata
from spot_on.playlists import sorted_positions


def test_backup_round_trip_preserves_unavailable_and_nontrack_entries(tmp_path: Path) -> None:
    playlist = PlaylistMetadata(
        id="0000000000000000000001", name="My playlist", snapshot_id="original-snapshot"
    )
    episode = PlaylistEntry(
        is_local=False,
        added_at="2024-01-01T00:00:00Z",
        item=Episode(
            type="episode",
            id="0000000000000000000002",
            uri="spotify:episode:0000000000000000000002",
            name="Episode",
        ),
    )
    entries = (episode, PlaylistEntry(is_local=True, item=None, added_at=None), episode)
    earliest = datetime.now(UTC)

    first_path = write_backup(tmp_path / "backups", playlist, entries)
    second_path = write_backup(tmp_path / "backups", playlist, entries)
    backup = read_backup(first_path)

    assert first_path != second_path
    assert read_backup(second_path).entries == entries
    assert backup.format_version == 1
    assert backup.playlist_id == playlist.id
    assert backup.playlist_name == playlist.name
    assert backup.snapshot_id == playlist.snapshot_id
    assert earliest <= backup.backed_up_at <= datetime.now(UTC)
    assert backup.entries == entries


def test_local_file_json_without_release_dates_survives_backup(tmp_path: Path) -> None:
    payload = (
        '{"is_local": true, "added_at": null, "item": {"type": "track", "id": null, '
        '"uri": "spotify:local:Artist:Album:Song:120", "name": "Song", '
        '"artists": [{"name": "Artist"}], "album": {"name": "Album", "images": []}}}'
    )
    entry = PlaylistEntry.model_validate_json(payload)
    playlist = PlaylistMetadata(id="0000000000000000000001", name="Playlist", snapshot_id="s")

    assert isinstance(entry.item, LocalTrack)
    assert entry.item.album.name == "Album"
    path = write_backup(tmp_path, playlist, (entry,))
    assert read_backup(path).entries == (entry,)
    with pytest.raises(PlaylistError, match="local file"):
        sorted_positions((entry,))

    nonlocal_payload = payload.replace('"is_local": true', '"is_local": false')
    catalog_payload = nonlocal_payload.replace(
        '"id": null', '"id": "0000000000000000000001"'
    ).replace("spotify:local:Artist:Album:Song:120", "spotify:track:0000000000000000000001")
    for invalid_payload in (nonlocal_payload, catalog_payload):
        with pytest.raises(ValidationError):
            PlaylistEntry.model_validate_json(invalid_payload)


@pytest.mark.parametrize(
    "content",
    (
        "{invalid json",
        '{"format_version": 2, "playlist_id": "0000000000000000000001", '
        '"playlist_name": "Playlist", "snapshot_id": "s", '
        '"backed_up_at": "2024-01-01T00:00:00Z", "entries": []}',
        '{"format_version": 1, "playlist_id": "0000000000000000000001"}',
    ),
)
def test_read_backup_rejects_corrupt_or_incompatible_data(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(BackupError, match="Invalid playlist backup"):
        read_backup(path)


def test_backup_io_errors_identify_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(BackupError, match=escape(str(missing))):
        read_backup(missing)

    blocked = tmp_path / "file"
    blocked.write_text("directory is a file", encoding="utf-8")
    playlist = PlaylistMetadata(id="0000000000000000000001", name="Playlist", snapshot_id="s")
    with pytest.raises(BackupError, match="Cannot write playlist backup"):
        write_backup(blocked, playlist, ())
