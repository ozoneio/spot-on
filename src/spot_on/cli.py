"""The shared CLI used locally and by workflows; credentials come only from the environment."""

import argparse
import logging
import os
import secrets
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from spot_on.backups import read_backup
from spot_on.batch import due_jobs, read_batch_config, run_batch, write_empty_batch_report
from spot_on.client import SpotifyClient
from spot_on.commands import (
    BackupOperation,
    CompareOperation,
    DuplicatesOperation,
    MergeOperation,
    PlaylistOperation,
    PlaylistResult,
    SortOperation,
    execute_operation,
)
from spot_on.commands import (
    DuplicateReport as DuplicateReport,
)
from spot_on.config import configured_playlist, playlist_identifier, required_environment
from spot_on.errors import ConfigurationError, SpotOnError
from spot_on.oauth import authorization_code, authorization_url, exchange_code
from spot_on.operations import restore_playlist
from spot_on.sorting import SortKey, SortOrder, default_sort_order


class Options(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    command: Literal[
        "auth-url",
        "auth-token",
        "duplicates",
        "backup",
        "sort",
        "restore",
        "compare",
        "merge",
        "batch",
        "batch-due",
    ]
    playlist_id: str | None
    backup_dir: Path
    backup_file: Path | None
    callback_url: str | None
    state: str | None
    sort_by: SortKey
    order: SortOrder | None
    other_playlist_id: str | None
    source_playlist_ids: list[str]
    config_file: Path | None
    report_dir: Path


class AuthorizationLink(BaseModel):
    url: str
    state: str


def print_json(result: BaseModel) -> None:
    """Keep piped JSON valid on terminals whose output encoding cannot represent track names."""
    print(result.model_dump_json(indent=2, ensure_ascii=True))


def parse_args(argv: Sequence[str]) -> Options:
    """Expose separate commands for each operation without credential arguments or prompts."""
    parser = argparse.ArgumentParser(description="Control Spotify playlists without a Spotify SDK.")
    parser.set_defaults(
        playlist_id=None,
        backup_dir=Path("backups"),
        backup_file=None,
        callback_url=None,
        state=None,
        sort_by="release-date",
        order=None,
        other_playlist_id=None,
        source_playlist_ids=[],
        config_file=None,
        report_dir=Path("reports"),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("auth-url", help="Create a one-time authorization URL and state.")
    token = subcommands.add_parser(
        "auth-token", help="Exchange an authorized callback for a token."
    )
    token.add_argument(
        "--callback-url", required=True, help="Full redirect URL after authorization."
    )
    token.add_argument("--state", required=True, help="State from the same auth-url invocation.")
    for command, description in (
        ("duplicates", "Report duplicate track candidates without removing any."),
        ("backup", "Save the playlist metadata and current order to JSON."),
        ("sort", "Back up and reorder tracks using a selected sort key."),
        ("restore", "Back up and restore a saved order with identical track membership."),
        ("compare", "Report tracks shared by or unique to two playlists."),
        ("merge", "Back up and append missing tracks from source playlists."),
    ):
        operation = subcommands.add_parser(command, help=description)
        operation.add_argument("--playlist-id", help="Spotify playlist ID, URI, or URL.")
        if command in ("backup", "sort", "restore", "merge"):
            operation.add_argument("--backup-dir", type=Path, default=Path("backups"))
        if command == "restore":
            operation.add_argument("--backup-file", type=Path, required=True)
        if command == "sort":
            operation.add_argument(
                "--by", dest="sort_by", choices=get_args(SortKey), default="release-date"
            )
            operation.add_argument("--order", choices=get_args(SortOrder))
        if command == "compare":
            operation.add_argument("--other-playlist-id", required=True)
        if command == "merge":
            operation.add_argument(
                "--source-playlist-id",
                dest="source_playlist_ids",
                action="append",
                required=True,
                help="Source playlist ID, URI, or URL; repeat for multiple sources.",
            )
    for command, description in (
        ("batch", "Execute every job in a validated JSON configuration."),
        ("batch-due", "Execute only jobs scheduled for the current UTC weekday and hour."),
    ):
        operation = subcommands.add_parser(command, help=description)
        operation.add_argument("--config", dest="config_file", type=Path, required=True)
        operation.add_argument("--backup-dir", type=Path, default=Path("backups"))
        operation.add_argument("--report-dir", type=Path, default=Path("reports"))
    return Options.model_validate(vars(parser.parse_args(argv)))


def print_authorization_link(environment: Mapping[str, str]) -> None:
    """Print a URL and its state for the user's one-time browser authorization."""
    client_id = required_environment(environment, "SPOTIFY_CLIENT_ID")
    redirect_uri = environment.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    state = secrets.token_urlsafe(32)
    link = AuthorizationLink(
        url=authorization_url(client_id, redirect_uri, state),
        state=state,
    )
    print_json(link)


def print_refresh_token(options: Options, environment: Mapping[str, str]) -> None:
    """Display the initial refresh token locally for the user to store as a secret."""
    client_id = required_environment(environment, "SPOTIFY_CLIENT_ID")
    client_secret = required_environment(environment, "SPOTIFY_CLIENT_SECRET")
    redirect_uri = environment.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if options.callback_url is None or options.state is None:
        raise ConfigurationError("auth-token requires --callback-url and --state from auth-url.")
    code = authorization_code(options.callback_url, redirect_uri, options.state)
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        token = exchange_code(http, client_id, client_secret, redirect_uri, code)
    print(f"SPOTIFY_REFRESH_TOKEN={token}")


def configured_client(http: httpx.Client, environment: Mapping[str, str]) -> SpotifyClient:
    """Read the shared credentials once for individual or batch operations."""
    client_id = required_environment(environment, "SPOTIFY_CLIENT_ID")
    client_secret = required_environment(environment, "SPOTIFY_CLIENT_SECRET")
    refresh_token = required_environment(environment, "SPOTIFY_REFRESH_TOKEN")
    return SpotifyClient(http, client_id, client_secret, refresh_token)


def playlist_operation(options: Options, environment: Mapping[str, str]) -> PlaylistOperation:
    """Turn command-line arguments into the same validated operation used in batch files."""
    playlist_id = configured_playlist(environment, options.playlist_id)
    match options.command:
        case "sort":
            order = (
                options.order if options.order is not None else default_sort_order(options.sort_by)
            )
            return SortOperation(
                command="sort", playlist_id=playlist_id, sort_by=options.sort_by, order=order
            )
        case "duplicates":
            return DuplicatesOperation(command="duplicates", playlist_id=playlist_id)
        case "backup":
            return BackupOperation(command="backup", playlist_id=playlist_id)
        case "compare":
            if options.other_playlist_id is None:
                raise ConfigurationError("compare requires --other-playlist-id.")
            return CompareOperation(
                command="compare",
                playlist_id=playlist_id,
                other_playlist_id=options.other_playlist_id,
            )
        case "merge":
            return MergeOperation(
                command="merge",
                playlist_id=playlist_id,
                source_playlist_ids=tuple(options.source_playlist_ids),
            )
        case _:
            raise ConfigurationError(f"Unsupported playlist command: {options.command}.")


def run_playlist_command(options: Options, environment: Mapping[str, str]) -> None:
    """Validate operation arguments before contacting Spotify."""
    operation = playlist_operation(options, environment)
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        result = execute_operation(
            configured_client(http, environment), operation, options.backup_dir
        )
    print_json(result)


def run_restore_command(options: Options, environment: Mapping[str, str]) -> None:
    """Validate the saved playlist identity before restoring its order."""
    if options.backup_file is None:
        raise ConfigurationError("restore requires --backup-file.")
    backup = read_backup(options.backup_file)
    playlist_id = playlist_identifier(backup.playlist_id)
    if options.playlist_id is not None and playlist_identifier(options.playlist_id) != playlist_id:
        raise ConfigurationError(
            f"--playlist-id must match the backup's playlist ID {playlist_id}."
        )
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        path = restore_playlist(configured_client(http, environment), backup, options.backup_dir)
    print_json(PlaylistResult(playlist_id=playlist_id, backup_file=str(path)))


def run_batch_jobs(options: Options, environment: Mapping[str, str]) -> None:
    """Execute all configured jobs manually after validating the entire file."""
    if options.config_file is None:
        raise ConfigurationError("batch requires --config.")
    config = read_batch_config(options.config_file)
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        summary = run_batch(
            configured_client(http, environment),
            config.jobs,
            options.backup_dir,
            options.report_dir,
        )
    print_json(summary)


def run_due_batch_jobs(options: Options, environment: Mapping[str, str]) -> None:
    """Use an explicit hourly schedule and avoid network requests when no jobs are due."""
    if options.config_file is None:
        raise ConfigurationError("batch-due requires --config.")
    config = read_batch_config(options.config_file)
    jobs = due_jobs(config, datetime.now(UTC))
    if not jobs:
        print_json(write_empty_batch_report(options.report_dir))
        return
    with httpx.Client(timeout=30.0, follow_redirects=False) as http:
        summary = run_batch(
            configured_client(http, environment), jobs, options.backup_dir, options.report_dir
        )
    print_json(summary)


def main() -> None:
    """Run a command, preserving a specific failure message and a nonzero exit status."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    options = parse_args(sys.argv[1:])
    environment = dict(os.environ)
    try:
        match options.command:
            case "auth-url":
                print_authorization_link(environment)
            case "auth-token":
                print_refresh_token(options, environment)
            case "restore":
                run_restore_command(options, environment)
            case "batch":
                run_batch_jobs(options, environment)
            case "batch-due":
                run_due_batch_jobs(options, environment)
            case _:
                run_playlist_command(options, environment)
    except SpotOnError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except ValidationError as error:
        print(
            "Error: Invalid command parameters: "
            + error.json(include_input=False, include_url=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
