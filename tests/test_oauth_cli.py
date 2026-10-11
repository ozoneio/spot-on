import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from spot_on.cli import AuthorizationLink, DuplicateReport
from spot_on.errors import ConfigurationError
from spot_on.oauth import authorization_code, authorization_url


def test_authorization_round_trip_preserves_callback_values() -> None:
    redirect_uri = "http://127.0.0.1:8888/callback"
    state = "random-state"
    code = "authorization+code&value"
    url = urlsplit(authorization_url("client-id", redirect_uri, state))
    query = parse_qs(url.query)

    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "accounts.spotify.com",
        "/authorize",
    )
    assert query["client_id"] == ["client-id"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["state"] == [state]
    callback = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
    assert authorization_code(callback, redirect_uri, state) == code


@pytest.mark.parametrize(
    "callback",
    (
        "http://127.0.0.1:8888/callback?code=secret-code&state=wrong-state",
        "http://127.0.0.1:8888/callback?code=secret-code&state=%C3%A9",
        "http://127.0.0.1:8888/callback?code=secret-code&state=random-state&state=random-state",
        "http://127.0.0.1:8888/callback?code=secret-code&code=other-code&state=random-state",
        "http://127.0.0.1:8888/callback?code=&state=random-state",
        "http://127.0.0.1:8888/callback?state=random-state&error=access_denied",
        "http://127.0.0.1:8888/other?code=secret-code&state=random-state",
        "http://127.0.0.2:8888/callback?code=secret-code&state=random-state",
        "http://127.0.0.1:8888/callback?code=secret-code&state=random-state#fragment",
        "http://[invalid/callback?code=secret-code&state=random-state",
    ),
)
def test_callback_rejects_invalid_destination_state_or_code_without_exposing_code(
    callback: str,
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        authorization_code(callback, "http://127.0.0.1:8888/callback", "random-state")

    assert "secret-code" not in str(raised.value)
    assert "random-state" not in str(raised.value)


@pytest.mark.parametrize(
    "redirect_uri",
    (
        "http://localhost:8888/callback",
        "http://example.com/callback",
        "http://127.0.0.1:invalid/callback",
        "http://[invalid/callback",
        "https://example.com/callback?query=1",
        "https://example.com/callback#fragment",
    ),
)
def test_authorization_rejects_invalid_redirect_configuration(redirect_uri: str) -> None:
    with pytest.raises(ConfigurationError):
        authorization_url("client-id", redirect_uri, "random-state")


def test_cli_module_and_installed_script_help() -> None:
    script = Path(sys.executable).with_name("spot-on.exe" if os.name == "nt" else "spot-on")
    for command in ((sys.executable, "-m", "spot_on", "--help"), (str(script), "--help")):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout


def test_cli_authorization_url_uses_configured_redirect_without_network() -> None:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("SPOTIFY_")
    }
    environment["SPOTIFY_CLIENT_ID"] = "client-id"
    environment["SPOTIFY_REDIRECT_URI"] = "http://127.0.0.1:8888/custom-callback"
    result = subprocess.run(
        (sys.executable, "-m", "spot_on", "auth-url"),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    link = AuthorizationLink.model_validate_json(result.stdout)
    query = parse_qs(urlsplit(link.url).query)
    assert query["redirect_uri"] == [environment["SPOTIFY_REDIRECT_URI"]]
    assert query["state"] == [link.state]
    assert link.state


def test_cli_json_preserves_unicode_with_ascii_stdout() -> None:
    program = (
        "from spot_on.cli import DuplicateReport, print_json\n"
        "from spot_on.playlists import DuplicateGroup\n"
        "uri = 'spotify:track:0000000000000000000001'\n"
        "group = DuplicateGroup(name='桜', artists=('歌手',), positions=(0, 1), uris=(uri, uri))\n"
        "print_json(DuplicateReport(playlist_id='playlist', entry_count=2, groups=(group,)))\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", program),
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        capture_output=True,
        text=True,
        encoding="ascii",
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = DuplicateReport.model_validate_json(result.stdout)
    assert report.groups[0].name == "桜"
    assert report.groups[0].artists == ("歌手",)
    assert report.groups[0].positions == (0, 1)


@pytest.mark.parametrize(
    ("configured", "missing"),
    (
        ({}, "SPOTIFY_CLIENT_ID"),
        ({"SPOTIFY_CLIENT_ID": "client-id"}, "SPOTIFY_CLIENT_SECRET"),
        (
            {"SPOTIFY_CLIENT_ID": "client-id", "SPOTIFY_CLIENT_SECRET": "secret-do-not-log"},
            "SPOTIFY_REFRESH_TOKEN",
        ),
    ),
)
def test_cli_missing_credentials_fail_before_network(
    configured: dict[str, str], missing: str
) -> None:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("SPOTIFY_")
    }
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "spot_on",
            "duplicates",
            "--playlist-id",
            "0000000000000000000001",
        ),
        env={**environment, **configured},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert missing in result.stderr
    assert "secret-do-not-log" not in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout
