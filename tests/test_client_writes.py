from collections import deque

import httpx
import pytest
from pydantic import ValidationError

from spot_on.client import AppendRequest, _write_snapshot
from spot_on.errors import SpotifyError, SpotifyOutcomeUncertainError
from spot_on.transport import request_append, request_reorder, response_error


def test_append_body_serializes_a_bounded_ordered_catalog_batch() -> None:
    uris = tuple(f"spotify:track:{number:022d}" for number in range(100))
    payload = AppendRequest(uris=uris)
    assert AppendRequest.model_validate_json(payload.model_dump_json()).uris == uris
    for invalid in ((), uris + (uris[0],), ("spotify:episode:0000000000000000000001",)):
        with pytest.raises(ValidationError):
            AppendRequest(uris=invalid)


def test_append_accepts_201_and_reorder_retains_200_protocol() -> None:
    for operation, status in ((request_append, 201), (request_reorder, 200)):
        response = httpx.Response(status, json={"snapshot_id": "new-snapshot"})
        callback = deque((response,)).popleft
        assert operation(callback, "playlist write", response_error_details, tuple) is response
        assert _write_snapshot(response, "playlist write", ()) == "new-snapshot"


def response_error_details(response: httpx.Response) -> SpotifyError:
    return response_error(response, "playlist write", ())


@pytest.mark.parametrize("status", (200, 204, 500, 503))
def test_append_never_replays_an_undocumented_success_or_server_failure(status: int) -> None:
    responses = deque((httpx.Response(status, text="write outcome"), httpx.Response(201)))
    with pytest.raises(SpotifyOutcomeUncertainError, match="not retried"):
        request_append(responses.popleft, "playlist write", response_error_details, tuple)
    assert len(responses) == 1


def test_append_retries_an_explicit_rate_limit_rejection() -> None:
    success = httpx.Response(201, json={"snapshot_id": "new-snapshot"})
    responses = deque((httpx.Response(429, headers={"Retry-After": "0"}), success))
    result = request_append(responses.popleft, "playlist write", response_error_details, tuple)
    assert result is success
    assert not responses


@pytest.mark.parametrize("body", ("{broken", "{}", '{"snapshot_id":""}'))
def test_accepted_write_without_valid_snapshot_is_uncertain(body: str) -> None:
    with pytest.raises(SpotifyOutcomeUncertainError, match="accepted the write"):
        _write_snapshot(httpx.Response(201, text=body), "playlist write", ())
