"""Explicit environment configuration and accepted Spotify playlist identifiers."""

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from spot_on.errors import ConfigurationError


def required_environment(environment: Mapping[str, str], name: str) -> str:
    """Require a nonempty value without including sensitive configuration in errors."""
    value = environment.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(
            f"{name} is required. Set it in your local environment or GitHub Actions secrets."
        )
    return value.strip()


def playlist_identifier(value: str) -> str:
    """Accept a Spotify playlist ID, playlist URI, or open.spotify.com playlist URL."""
    identifier = value.strip()
    if identifier.startswith("spotify:playlist:"):
        identifier = identifier.removeprefix("spotify:playlist:")
    elif identifier.startswith("https://"):
        try:
            url = urlsplit(identifier)
        except ValueError as error:
            raise ConfigurationError(
                "Playlist URLs must use https://open.spotify.com/playlist/ID."
            ) from error
        if url.hostname != "open.spotify.com" or url.netloc != "open.spotify.com":
            raise ConfigurationError("Playlist URLs must use https://open.spotify.com/playlist/ID.")
        match = re.fullmatch(r"/playlist/([A-Za-z0-9]{22})/?", url.path)
        if match is None:
            raise ConfigurationError("Playlist URLs must use https://open.spotify.com/playlist/ID.")
        identifier = match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", identifier) is None:
        raise ConfigurationError(
            "Playlist must be a 22-character Spotify ID, spotify:playlist:ID, "
            "or https://open.spotify.com/playlist/ID URL."
        )
    return identifier


def configured_playlist(environment: Mapping[str, str], value: str | None) -> str:
    """Use an explicit argument or the configured playlist variable."""
    identifier = (
        value if value is not None else required_environment(environment, "SPOTIFY_PLAYLIST_ID")
    )
    return playlist_identifier(identifier)
