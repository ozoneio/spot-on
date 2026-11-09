from types import SimpleNamespace

import pytest

from spot_on import operations
from spot_on.errors import PlaylistError


class FakeClient:
    def __init__(self, *snapshots: str) -> None:
        self.snapshots = list(snapshots)

    def playlist(self, playlist_id: str) -> SimpleNamespace:
        return SimpleNamespace(snapshot_id=self.snapshots.pop(0))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operations.time, "sleep", lambda _: None)


def test_stale_read_after_write_is_retried() -> None:
    operations.assert_snapshot(FakeClient("old", "old", "new"), "p", "new")  # type: ignore[arg-type]


def test_apply_order_chains_write_snapshots_without_stale_reads() -> None:
    sent: list[str] = []

    class WriteOnlyClient(FakeClient):
        def reorder(self, playlist_id: str, start: int, before: int, snapshot_id: str) -> str:
            sent.append(snapshot_id)
            return f"v{len(sent)}"

    # FakeClient() has no snapshots queued, so any GET check would raise IndexError.
    operations.apply_order(WriteOnlyClient(), "p", "v0", (2, 0, 1))  # type: ignore[arg-type]
    assert sent and sent == [f"v{i}" for i in range(len(sent))]


def test_persistent_mismatch_still_fails() -> None:
    with pytest.raises(PlaylistError, match="changed concurrently"):
        operations.assert_snapshot(FakeClient(*["other"] * 4), "p", "new")  # type: ignore[arg-type]
