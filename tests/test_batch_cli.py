import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

import httpx
import pytest

from spot_on.backups import write_backup
from spot_on.batch import (
    BatchConfig,
    BatchSummary,
    CompletedJob,
    FailedJob,
    PendingJob,
    checkpoint_job,
    due_jobs,
    job_report_path,
    read_batch_config,
    run_batch,
    write_report,
)
from spot_on.cli import parse_args, playlist_operation
from spot_on.client import SpotifyClient
from spot_on.commands import (
    CompareOperation,
    DuplicateReport,
    MergeOperation,
    PlaylistResult,
    SortOperation,
)
from spot_on.errors import BatchError, ConfigurationError
from spot_on.models import PlaylistMetadata
from spot_on.playlists import DuplicateGroup
from spot_on.sorting import SortKey, SortOrder

LEFT = "0000000000000000000001"
RIGHT = "0000000000000000000002"
THIRD = "0000000000000000000003"


def write_jobs(path: Path, jobs: tuple[str, ...]) -> None:
    path.write_text('{"jobs": [' + ",".join(jobs) + "]}", encoding="utf-8")


def run_cli(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("SPOTIFY_")
    }
    return subprocess.run(
        (sys.executable, "-m", "spot_on", *arguments),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_batch_configuration_normalizes_all_operation_identifiers(tmp_path: Path) -> None:
    path = tmp_path / "batch.json"
    write_jobs(
        path,
        (
            '{"id":"sort","operation":{"command":"sort",'
            f'"playlist_id":"spotify:playlist:{LEFT}","sort_by":"artist","order":"asc"}}}}',
            f'{{"id":"duplicates","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
            '{"id":"backup","operation":{"command":"backup",'
            f'"playlist_id":"https://open.spotify.com/playlist/{LEFT}?si=share"}}}}',
            '{"id":"compare","operation":{"command":"compare",'
            f'"playlist_id":"{LEFT}","other_playlist_id":"spotify:playlist:{RIGHT}"}}}}',
            '{"id":"merge","operation":{"command":"merge",'
            f'"playlist_id":"{LEFT}","source_playlist_ids":["spotify:playlist:{RIGHT}",'
            f'"https://open.spotify.com/playlist/{THIRD}"]}}}}',
        ),
    )

    config = read_batch_config(path)

    assert tuple(job.id for job in config.jobs) == (
        "sort",
        "duplicates",
        "backup",
        "compare",
        "merge",
    )
    assert all(job.operation.playlist_id == LEFT for job in config.jobs)
    comparison = config.jobs[3].operation
    merge = config.jobs[4].operation
    assert isinstance(comparison, CompareOperation)
    assert comparison.other_playlist_id == RIGHT
    assert isinstance(merge, MergeOperation)
    assert merge.source_playlist_ids == (RIGHT, THIRD)


@pytest.mark.parametrize(
    "invalid_job",
    (
        f'{{"id":"first","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
        f'{{"id":"../escape","operation":{{"command":"backup","playlist_id":"{LEFT}"}}}}',
        '{"id":"invalid","schedule":{"weekdays":["mon"],"hour_utc":24},'
        f'"operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
        '{"id":"invalid","operation":{"command":"merge",'
        f'"playlist_id":"{LEFT}","source_playlist_ids":["{RIGHT}","spotify:playlist:{RIGHT}"]}}}}',
        '{"id":"invalid","operation":{"command":"merge",'
        f'"playlist_id":"{LEFT}","source_playlist_ids":"{RIGHT}"}}}}',
    ),
)
def test_batch_validates_every_job_before_credentials(tmp_path: Path, invalid_job: str) -> None:
    path = tmp_path / "batch.json"
    reports = tmp_path / "reports"
    write_jobs(
        path,
        (
            f'{{"id":"first","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
            invalid_job,
        ),
    )

    with pytest.raises(ConfigurationError, match="Invalid batch configuration"):
        read_batch_config(path)
    result = run_cli(("batch", "--config", str(path), "--report-dir", str(reports)))

    assert result.returncode == 1
    assert "Invalid batch configuration" in result.stderr
    assert "SPOTIFY_CLIENT_ID" not in result.stderr
    assert "Traceback" not in result.stderr
    assert not reports.exists()


def test_batch_cli_missing_configuration_has_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    result = run_cli(("batch", "--config", str(path)))

    assert result.returncode == 1
    assert str(path) in result.stderr
    assert "Cannot read batch configuration" in result.stderr
    assert "Traceback" not in result.stderr


def test_scheduling_uses_utc_hour_and_preserves_order_without_manual_jobs() -> None:
    config = BatchConfig.model_validate_json(
        '{"jobs": ['
        '{"id":"second","schedule":{"weekdays":["mon"],"hour_utc":0},'
        f'"operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}},'
        f'{{"id":"manual","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}},'
        '{"id":"first","schedule":{"weekdays":["mon","fri"],"hour_utc":0},'
        f'"operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}},'
        '{"id":"later","schedule":{"weekdays":["mon"],"hour_utc":1},'
        f'"operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}]}}'
    )
    malaysia = timezone(timedelta(hours=8))
    now = datetime(2026, 1, 5, 8, 59, tzinfo=malaysia)

    assert tuple(job.id for job in due_jobs(config, now)) == ("second", "first")
    assert due_jobs(config, now) == due_jobs(config, now.astimezone(UTC))
    assert due_jobs(config, datetime(2026, 1, 5, 7, 59, tzinfo=malaysia)) == ()
    assert tuple(job.id for job in due_jobs(config, datetime(2026, 1, 5, 1, tzinfo=UTC))) == (
        "later",
    )
    with pytest.raises(ConfigurationError, match="timezone-aware"):
        due_jobs(config, datetime(2026, 1, 5, 0))


def test_report_replacement_preserves_typed_results_and_unicode(tmp_path: Path) -> None:
    path = tmp_path / "reports" / "summary.json"
    started = datetime(2026, 1, 5, tzinfo=UTC)
    running = BatchSummary(
        started_at=started, status="running", selected_job_ids=("done", "failed"), jobs=()
    )
    completed = CompletedJob(
        id="done",
        command="duplicates",
        status="completed",
        result=DuplicateReport(
            playlist_id=LEFT,
            entry_count=2,
            groups=(
                DuplicateGroup(name="桜", artists=("歌手",), positions=(0, 1), uris=("a", "b")),
            ),
        ),
    )
    failed = FailedJob(id="failed", command="backup", status="failed", error="Cannot write 保存")
    final = BatchSummary(
        started_at=started,
        status="failed",
        selected_job_ids=("done", "failed"),
        jobs=(completed, failed),
    )

    write_report(path, running)
    assert BatchSummary.model_validate_json(path.read_text(encoding="utf-8")) == running
    write_report(path, final)

    assert BatchSummary.model_validate_json(path.read_text(encoding="utf-8")) == final
    assert tuple(path.parent.glob("*.tmp")) == ()


def test_report_write_failure_is_explicit(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("file", encoding="utf-8")
    report = BatchSummary(
        started_at=datetime.now(UTC), status="completed", selected_job_ids=(), jobs=()
    )

    with pytest.raises(BatchError, match="Cannot save batch report"):
        write_report(blocked / "summary.json", report)


def test_batch_due_with_manual_only_jobs_writes_empty_summary_without_credentials(
    tmp_path: Path,
) -> None:
    path = tmp_path / "batch.json"
    reports = tmp_path / "reports"
    write_jobs(
        path,
        (f'{{"id":"manual","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',),
    )
    result = run_cli(("batch-due", "--config", str(path), "--report-dir", str(reports)))

    assert result.returncode == 0, result.stderr
    summary = BatchSummary.model_validate_json(result.stdout)
    assert summary.status == "completed"
    assert summary.selected_job_ids == ()
    assert summary.jobs == ()
    assert (
        BatchSummary.model_validate_json((reports / "summary.json").read_text(encoding="utf-8"))
        == summary
    )


@pytest.mark.parametrize(
    ("arguments", "key", "order"),
    (
        ((), "release-date", "desc"),
        (("--by", "title"), "title", "asc"),
        (("--by", "duration"), "duration", "asc"),
        (("--by", "artist", "--order", "desc"), "artist", "desc"),
    ),
)
def test_sort_cli_constructs_validated_operation(
    arguments: tuple[str, ...], key: SortKey, order: SortOrder
) -> None:
    options = parse_args(("sort", "--playlist-id", LEFT, *arguments))
    operation = playlist_operation(options, {})

    assert isinstance(operation, SortOperation)
    assert operation.sort_by == key
    assert operation.order == order


def test_compare_and_merge_cli_normalize_argument_identifiers() -> None:
    compare = playlist_operation(
        parse_args(
            ("compare", "--playlist-id", LEFT, "--other-playlist-id", f"spotify:playlist:{RIGHT}")
        ),
        {},
    )
    merge = playlist_operation(
        parse_args(
            (
                "merge",
                "--playlist-id",
                LEFT,
                "--source-playlist-id",
                f"spotify:playlist:{RIGHT}",
                "--source-playlist-id",
                f"https://open.spotify.com/playlist/{THIRD}",
            )
        ),
        {},
    )

    assert isinstance(compare, CompareOperation)
    assert compare.other_playlist_id == RIGHT
    assert isinstance(merge, MergeOperation)
    assert merge.source_playlist_ids == (RIGHT, THIRD)


def test_compare_cli_requires_other_playlist() -> None:
    result = run_cli(("compare", "--playlist-id", LEFT))

    assert result.returncode == 2
    assert "--other-playlist-id" in result.stderr
    assert "Traceback" not in result.stderr


def test_merge_cli_rejects_destination_source_alias_before_credentials() -> None:
    result = run_cli(
        ("merge", "--playlist-id", LEFT, "--source-playlist-id", f"spotify:playlist:{LEFT}")
    )

    assert result.returncode == 1
    assert "destination cannot also be a source" in result.stderr
    assert "SPOTIFY_CLIENT_ID" not in result.stderr
    assert "Traceback" not in result.stderr


def test_job_report_names_are_portable_and_ids_are_casefold_unique(tmp_path: Path) -> None:
    config_path = tmp_path / "batch.json"
    write_jobs(
        config_path,
        (
            f'{{"id":"CON","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
            f'{{"id":"NUL","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
        ),
    )
    config = read_batch_config(config_path)
    for job in config.jobs:
        report = PendingJob(id=job.id, command=job.operation.command, status="pending")
        path = job_report_path(tmp_path / "reports", job.id)
        assert not PureWindowsPath(path.name).is_reserved()
        write_report(path, report)
        assert PendingJob.model_validate_json(path.read_text(encoding="utf-8")) == report

    write_jobs(
        config_path,
        (
            f'{{"id":"CON","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
            f'{{"id":"con","operation":{{"command":"duplicates","playlist_id":"{LEFT}"}}}}',
        ),
    )
    with pytest.raises(ConfigurationError, match="unique ignoring letter case"):
        read_batch_config(config_path)


def test_batch_preflights_report_directory_before_using_real_closed_connector(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "jobs").write_text("directory blocked by a file", encoding="utf-8")
    config = BatchConfig.model_validate_json(
        '{"jobs":[{"id":"sort","operation":{"command":"sort",'
        f'"playlist_id":"{LEFT}","sort_by":"artist","order":"asc"}}}}]}}'
    )
    http = httpx.Client()
    http.close()
    client = SpotifyClient(http, "unused-client", "unused-secret", "unused-refresh")
    backups = tmp_path / "backups"

    with pytest.raises(BatchError, match="No batch operations were executed"):
        run_batch(client, config.jobs, backups, reports)

    summary = BatchSummary.model_validate_json(
        (reports / "summary.json").read_text(encoding="utf-8")
    )
    assert summary.status == "failed"
    assert summary.selected_job_ids == ("sort",)
    assert len(summary.jobs) == 1
    assert isinstance(summary.jobs[0], FailedJob)
    assert summary.jobs[0].id == "sort"
    assert "Cannot save batch report" in summary.jobs[0].error
    assert not backups.exists()


def test_failed_checkpoint_retains_completed_operation_and_real_backup(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "jobs").write_text("directory blocked by a file", encoding="utf-8")
    backup = write_backup(
        tmp_path / "backups",
        PlaylistMetadata(id=LEFT, name="Playlist", snapshot_id="original"),
        (),
    )
    completed = CompletedJob(
        id="sort",
        command="sort",
        status="completed",
        result=PlaylistResult(playlist_id=LEFT, backup_file=str(backup)),
    )
    summary = BatchSummary(
        started_at=datetime.now(UTC),
        status="running",
        selected_job_ids=("sort",),
        jobs=(completed,),
    )

    with pytest.raises(BatchError, match="Cannot checkpoint batch job") as raised:
        checkpoint_job(reports, completed, summary)

    saved = BatchSummary.model_validate_json((reports / "summary.json").read_text(encoding="utf-8"))
    assert saved.status == "failed"
    assert saved.started_at == summary.started_at
    assert saved.jobs == (completed,)
    assert backup.is_file()
    assert backup.name in str(raised.value)
    assert "Cannot save batch report" in str(raised.value)
