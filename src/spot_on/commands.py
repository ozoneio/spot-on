"""Validated playlist operations shared by individual commands and configured batches."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from spot_on.backups import write_backup
from spot_on.client import SpotifyClient
from spot_on.collections import MergeResult, PlaylistComparison, compare_playlists, merge_playlists
from spot_on.config import playlist_identifier
from spot_on.models import SpotifyModel
from spot_on.operations import read_playlist, sort_playlist
from spot_on.playlists import DuplicateGroup, duplicate_groups
from spot_on.sorting import SortKey, SortOrder


class PlaylistOperationModel(SpotifyModel):
    playlist_id: str

    @field_validator("playlist_id")
    @classmethod
    def _normalize_playlist_id(cls, value: str) -> str:
        return playlist_identifier(value)


class SortOperation(PlaylistOperationModel):
    command: Literal["sort"]
    sort_by: SortKey
    order: SortOrder


class DuplicatesOperation(PlaylistOperationModel):
    command: Literal["duplicates"]


class BackupOperation(PlaylistOperationModel):
    command: Literal["backup"]


class CompareOperation(PlaylistOperationModel):
    command: Literal["compare"]
    other_playlist_id: str

    @field_validator("other_playlist_id")
    @classmethod
    def _normalize_other_playlist_id(cls, value: str) -> str:
        return playlist_identifier(value)


class MergeOperation(PlaylistOperationModel):
    command: Literal["merge"]
    source_playlist_ids: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("source_playlist_ids")
    @classmethod
    def _normalize_source_playlist_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(playlist_identifier(value) for value in values)

    @model_validator(mode="after")
    def _require_distinct_sources(self) -> Self:
        if len(set(self.source_playlist_ids)) != len(self.source_playlist_ids):
            raise ValueError("Merge source playlist IDs must be distinct.")
        if self.playlist_id in self.source_playlist_ids:
            raise ValueError("The merge destination cannot also be a source playlist.")
        return self


PlaylistOperation = Annotated[
    SortOperation | DuplicatesOperation | BackupOperation | CompareOperation | MergeOperation,
    Field(discriminator="command"),
]


class PlaylistResult(SpotifyModel):
    playlist_id: str
    backup_file: str


class DuplicateReport(SpotifyModel):
    playlist_id: str
    entry_count: int
    groups: tuple[DuplicateGroup, ...]


OperationResult = PlaylistResult | DuplicateReport | PlaylistComparison | MergeResult


def execute_operation(
    client: SpotifyClient, operation: PlaylistOperation, backup_directory: Path
) -> OperationResult:
    """Execute an already validated operation using the same path in local and automated runs."""
    match operation:
        case SortOperation():
            path = sort_playlist(
                client, operation.playlist_id, backup_directory, operation.sort_by, operation.order
            )
            return PlaylistResult(playlist_id=operation.playlist_id, backup_file=str(path))
        case DuplicatesOperation():
            _, entries = read_playlist(client, operation.playlist_id)
            return DuplicateReport(
                playlist_id=operation.playlist_id,
                entry_count=len(entries),
                groups=duplicate_groups(entries),
            )
        case BackupOperation():
            playlist, entries = read_playlist(client, operation.playlist_id)
            path = write_backup(backup_directory, playlist, entries)
            return PlaylistResult(playlist_id=operation.playlist_id, backup_file=str(path))
        case CompareOperation():
            return compare_playlists(client, operation.playlist_id, operation.other_playlist_id)
        case MergeOperation():
            return merge_playlists(
                client, operation.playlist_id, operation.source_playlist_ids, backup_directory
            )
