"""Spotify authorization-code helpers without a Spotify SDK or token cache."""

import ipaddress
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import ValidationError

from spot_on.errors import ConfigurationError, SpotifyError
from spot_on.models import AuthorizationTokenResponse, OAuthErrorResponse, TokenResponse
from spot_on.transport import redact, request_with_retries

TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public"
)


def _require_nonempty(value: str, name: str) -> None:
    """Reject missing credentials without echoing their values."""
    if not value or value != value.strip():
        raise ConfigurationError(f"{name} must be nonempty with no surrounding whitespace.")


def _validate_redirect_uri(redirect_uri: str) -> None:
    """Require HTTPS, or HTTP with an explicit loopback IP, as Spotify requires."""
    _require_nonempty(redirect_uri, "Spotify redirect URI")
    if any(character.isspace() for character in redirect_uri):
        raise ConfigurationError("Spotify redirect URI must not contain whitespace.")
    try:
        uri = urlsplit(redirect_uri)
    except ValueError as error:
        raise ConfigurationError("Spotify redirect URI has an invalid URL authority.") from error
    if (
        uri.hostname is None
        or uri.username is not None
        or uri.password is not None
        or uri.query
        or uri.fragment
    ):
        raise ConfigurationError(
            "Spotify redirect URI must have a host and no credentials, query, or fragment."
        )
    try:
        _ = uri.port
    except ValueError as error:
        raise ConfigurationError("Spotify redirect URI has an invalid port.") from error
    if uri.scheme == "https":
        return
    if uri.scheme == "http":
        try:
            address = ipaddress.ip_address(uri.hostname)
        except ValueError as error:
            raise ConfigurationError(
                "HTTP Spotify redirect URIs require an explicit loopback IP, such as "
                "http://127.0.0.1:8888/callback; localhost is not accepted by Spotify."
            ) from error
        if address.is_loopback:
            return
    raise ConfigurationError("Spotify redirect URI must use HTTPS or HTTP with a loopback IP.")


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the authorization request for reading and modifying user playlists."""
    _require_nonempty(client_id, "Spotify client ID")
    _require_nonempty(state, "OAuth state")
    _validate_redirect_uri(redirect_uri)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPES,
        }
    )
    return f"https://accounts.spotify.com/authorize?{query}"


def authorization_code(callback_url: str, redirect_uri: str, state: str) -> str:
    """Validate the callback destination and state before returning its single code."""
    _validate_redirect_uri(redirect_uri)
    _require_nonempty(state, "OAuth state")
    try:
        callback = urlsplit(callback_url)
    except ValueError as error:
        raise ConfigurationError("OAuth callback has an invalid URL authority.") from error
    redirect = urlsplit(redirect_uri)
    if (callback.scheme, callback.netloc, callback.path) != (
        redirect.scheme,
        redirect.netloc,
        redirect.path,
    ) or callback.fragment:
        raise ConfigurationError("OAuth callback does not match the configured redirect URI.")
    try:
        query = parse_qs(callback.query, keep_blank_values=True, errors="strict")
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            "OAuth callback contains invalid UTF-8 query parameters."
        ) from error
    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0].encode(), state.encode()):
        raise ConfigurationError("OAuth callback must contain exactly one matching state value.")
    errors = query.get("error", [])
    codes = query.get("code", [])
    if errors:
        if len(errors) != 1 or not errors[0] or codes:
            raise ConfigurationError(
                "OAuth callback contains invalid or conflicting error parameters."
            )
        raise ConfigurationError(
            "Spotify authorization failed. Grant playlist permissions and restart authorization; "
            f"OAuth error: {errors[0]}."
        )
    if len(codes) != 1 or not codes[0]:
        raise ConfigurationError(
            "OAuth callback must contain exactly one nonempty authorization code."
        )
    return codes[0]


def _token_error(
    response: httpx.Response, context: str, credentials: tuple[str, ...]
) -> SpotifyError:
    """Display only OAuth error fields, never a token-bearing response body."""
    try:
        details = OAuthErrorResponse.model_validate_json(response.content)
    except ValidationError:
        body = "[redacted: response is not a valid OAuth error object]"
    else:
        body = redact(details.model_dump_json(), credentials)
    return SpotifyError(
        f"{context} failed: HTTP {response.status_code}; response body: {body}. "
        "For invalid_grant, authorize again and replace SPOTIFY_REFRESH_TOKEN; "
        "for invalid_client, verify SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
    )


def request_token(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    data: dict[str, str],
) -> httpx.Response:
    """Post a redacted OAuth token request with bounded transient-error retries."""
    _require_nonempty(client_id, "Spotify client ID")
    _require_nonempty(client_secret, "Spotify client secret")
    context = f"POST {TOKEN_URL} (grant_type={data['grant_type']}; credentials redacted)"
    credentials = (client_secret, *data.values())
    return request_with_retries(
        lambda: http.post(
            TOKEN_URL,
            auth=httpx.BasicAuth(client_id, client_secret),
            data=data,
            follow_redirects=False,
        ),
        context,
        lambda response: _token_error(response, context, credentials),
        lambda: credentials,
    )


def parse_token(response: httpx.Response) -> TokenResponse:
    """Validate token metadata while excluding credentials from validation errors."""
    try:
        return TokenResponse.model_validate_json(response.content)
    except ValidationError as error:
        raise SpotifyError(
            "Spotify token response has invalid required fields; HTTP "
            f"{response.status_code}; validation: "
            f"{error.json(include_input=False, include_url=False)}; "
            "response body redacted to protect credentials."
        ) from None


def exchange_code(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> str:
    """Exchange a validated callback code for the refresh token saved by the user."""
    _validate_redirect_uri(redirect_uri)
    _require_nonempty(code, "Spotify authorization code")
    response = request_token(
        http,
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code},
    )
    try:
        token = AuthorizationTokenResponse.model_validate_json(response.content)
    except ValidationError as error:
        raise SpotifyError(
            "Spotify authorization response has invalid required fields; HTTP "
            f"{response.status_code}; validation: "
            f"{error.json(include_input=False, include_url=False)}; "
            "response body redacted to protect credentials."
        ) from None
    return token.refresh_token
