"""Validate batch configuration, select UTC schedule slots, and checkpoint sequential results."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, model_validator

from spot_on.client import SpotifyClient
from spot_on.commands import OperationResult, PlaylistOperation, execute_operation
from spot_on.errors import BatchError, ConfigurationError, SpotOnError
from spot_on.models import SpotifyModel

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OperationName = Literal["sort", "duplicates", "backup", "compare", "merge"]


class JobSchedule(SpotifyModel):
    """Run during the given UTC hour on selected weekdays; unscheduled jobs are manual only."""

    weekdays: Annotated[tuple[Weekday, ...], Field(min_length=1, max_length=7)]
    hour_utc: Annotated[int, Field(ge=0, le=23)]


class BatchJob(SpotifyModel):
    id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    schedule: JobSchedule | None = None
    operation: PlaylistOperation


class BatchConfig(SpotifyModel):
    jobs: Annotated[tuple[BatchJob, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _require_unique_job_ids(self) -> Self:
        identifiers = tuple(job.id.casefold() for job in self.jobs)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(
                "Batch job IDs must be unique ignoring letter case to protect reports."
            )
        return self


class CompletedJob(SpotifyModel):
    id: str
    command: OperationName
    status: Literal["completed"]
    result: OperationResult


class FailedJob(SpotifyModel):
    id: str
    command: OperationName
    status: Literal["failed"]
    error: str


class PendingJob(SpotifyModel):
    id: str
    command: OperationName
    status: Literal["pending"]


JobResult = Annotated[CompletedJob | FailedJob, Field(discriminator="status")]


class BatchSummary(SpotifyModel):
    started_at: datetime
    status: Literal["running", "completed", "failed"]
    selected_job_ids: tuple[str, ...]
    jobs: tuple[JobResult, ...]


def read_batch_config(path: Path) -> BatchConfig:
    """Validate all operation parameters and IDs before requesting any Spotify credentials."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(f"Cannot read batch configuration at {path}: {error}.") from error
    try:
        return BatchConfig.model_validate_json(content)
    except (ValidationError, ConfigurationError) as error:
        raise ConfigurationError(f"Invalid batch configuration at {path}: {error}.") from error


def due_jobs(config: BatchConfig, now: datetime) -> tuple[BatchJob, ...]:
    """Select the current UTC hour without inventing a schedule for manual-only jobs."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigurationError("Batch scheduling requires a timezone-aware current datetime.")
    current = now.astimezone(UTC)
    names: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    weekday = names[current.weekday()]
    return tuple(
        job
        for job in config.jobs
        if job.schedule is not None
        and weekday in job.schedule.weekdays
        and current.hour == job.schedule.hour_utc
    )


def write_report(path: Path, report: BaseModel) -> None:
    """Atomically replace a UTF-8 checkpoint so interruption preserves the preceding report."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise BatchError(f"Cannot save batch report at {path}: {error}.") from error


def write_empty_batch_report(report_directory: Path) -> BatchSummary:
    """Record a scheduled invocation with no due jobs without contacting Spotify."""
    summary = BatchSummary(
        started_at=datetime.now(UTC), status="completed", selected_job_ids=(), jobs=()
    )
    write_report(report_directory / "summary.json", summary)
    return summary


def job_report_path(report_directory: Path, job_id: str) -> Path:
    """Prefix validated IDs so names such as CON remain ordinary filenames on Windows."""
    return report_directory / "jobs" / f"job-{job_id}.json"


def checkpoint_job(report_directory: Path, report: JobResult, summary: BatchSummary) -> None:
    """Retain the operation result or original failure when a later report write fails."""
    try:
        write_report(job_report_path(report_directory, report.id), report)
        write_report(report_directory / "summary.json", summary)
    except BatchError as error:
        failed_summary = BatchSummary(
            started_at=summary.started_at,
            status="failed",
            selected_job_ids=summary.selected_job_ids,
            jobs=summary.jobs,
        )
        details = report.model_dump_json(ensure_ascii=True)
        try:
            write_report(report_directory / "summary.json", failed_summary)
        except BatchError as summary_error:
            raise BatchError(
                f"Cannot checkpoint batch job {report.id!r}. Operation details: {details}. "
                f"Report error: {error}. Summary error: {summary_error}."
            ) from summary_error
        raise BatchError(
            f"Cannot checkpoint batch job {report.id!r}: {error}. "
            f"Operation details: {details}. See {report_directory / 'summary.json'}."
        ) from error


def run_batch(
    client: SpotifyClient,
    jobs: tuple[BatchJob, ...],
    backup_directory: Path,
    report_directory: Path,
) -> BatchSummary:
    """Run jobs in listed order, checkpoint progress, and stop at the first explicit failure."""
    started_at = datetime.now(UTC)
    selected = tuple(job.id for job in jobs)
    results: list[JobResult] = []
    summary = BatchSummary(
        started_at=started_at, status="running", selected_job_ids=selected, jobs=()
    )
    write_report(report_directory / "summary.json", summary)
    for job in jobs:
        pending = PendingJob(id=job.id, command=job.operation.command, status="pending")
        try:
            write_report(job_report_path(report_directory, job.id), pending)
        except BatchError as error:
            failed = FailedJob(
                id=job.id, command=job.operation.command, status="failed", error=str(error)
            )
            summary = BatchSummary(
                started_at=started_at, status="failed", selected_job_ids=selected, jobs=(failed,)
            )
            write_report(report_directory / "summary.json", summary)
            raise BatchError(
                f"Cannot prepare report for batch job {job.id!r}: {error}. "
                "No batch operations were executed."
            ) from error
    for job in jobs:
        try:
            result = execute_operation(client, job.operation, backup_directory)
        except SpotOnError as error:
            failed = FailedJob(
                id=job.id, command=job.operation.command, status="failed", error=str(error)
            )
            results.append(failed)
            summary = BatchSummary(
                started_at=started_at,
                status="failed",
                selected_job_ids=selected,
                jobs=tuple(results),
            )
            checkpoint_job(report_directory, failed, summary)
            raise BatchError(
                f"Batch job {job.id!r} ({job.operation.command}) failed: {error}. "
                f"Completed jobs were retained; see {report_directory / 'summary.json'}."
            ) from error
        completed = CompletedJob(
            id=job.id, command=job.operation.command, status="completed", result=result
        )
        results.append(completed)
        summary = BatchSummary(
            started_at=started_at,
            status="running",
            selected_job_ids=selected,
            jobs=tuple(results),
        )
        checkpoint_job(report_directory, completed, summary)
    summary = BatchSummary(
        started_at=started_at, status="completed", selected_job_ids=selected, jobs=tuple(results)
    )
    write_report(report_directory / "summary.json", summary)
    return summary
