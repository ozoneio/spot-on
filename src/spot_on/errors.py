"""Actionable errors shared by the command line and Spotify connector."""


class SpotOnError(Exception):
    """An operation could not complete and needs user attention."""


class ConfigurationError(SpotOnError):
    """Required configuration or an authorization callback is invalid."""


class SpotifyError(SpotOnError):
    """Spotify rejected a request or returned an invalid response."""


class SpotifyTransportError(SpotifyError):
    """A Spotify request failed after its permitted transport retries."""


class SpotifyRateLimitError(SpotifyError):
    """Spotify's rate limit cannot be retried within the bounded wait."""


class SpotifyOutcomeUncertainError(SpotifyError):
    """A playlist write may have succeeded and must not be replayed blindly."""


class PlaylistError(SpotOnError):
    """A playlist cannot safely undergo the requested operation."""


class BackupError(SpotOnError):
    """A playlist backup could not be read, written, or restored."""


class BatchError(SpotOnError):
    """A configured job failed after its batch reports were saved."""
