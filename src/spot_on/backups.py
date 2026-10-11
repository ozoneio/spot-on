"""Versioned playlist metadata backups used to restore track order."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from spot_on.errors import BackupError
from spot_on.models import PlaylistEntry, PlaylistMetadata


class PlaylistBackup(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    format_version: Literal[1]
    playlist_id: str
    playlist_name: str
    snapshot_id: str
    backed_up_at: datetime
    entries: tuple[PlaylistEntry, ...]


def write_backup(
    directory: Path, playlist: PlaylistMetadata, entries: tuple[PlaylistEntry, ...]
) -> Path:
    """Write a unique backup before any mutation, leaving older recovery files intact."""
    now = datetime.now(UTC)
    backup = PlaylistBackup(
        format_version=1,
        playlist_id=playlist.id,
        playlist_name=playlist.name,
        snapshot_id=playlist.snapshot_id,
        backed_up_at=now,
        entries=entries,
    )
    path = directory / f"playlist-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(backup.model_dump_json(indent=2))
            output.write("\n")
    except OSError as error:
        raise BackupError(f"Cannot write playlist backup at {path}: {error}.") from error
    return path


def read_backup(path: Path) -> PlaylistBackup:
    """Validate the complete current backup schema before contacting Spotify."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise BackupError(f"Cannot read playlist backup at {path}: {error}.") from error
    try:
        return PlaylistBackup.model_validate_json(content)
    except ValidationError as error:
        raise BackupError(f"Invalid playlist backup at {path}: {error}.") from error
