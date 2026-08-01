"""Origin and client-intent guard for passwordless local API writes.

This module intentionally contains no credential, token, challenge or password
logic. It only makes state-changing calls deliberate.
"""

from urllib.parse import urlparse

from fastapi import HTTPException, Request


_ALLOWED_CLIENTS = {"ui", "bcmctl"}


def _client_has_intent(request: Request) -> bool:
    client = request.headers.get("X-bcMeter-Client", "")
    return client.strip().lower() in _ALLOWED_CLIENTS


def _request_is_same_origin(request: Request) -> bool:
    if request.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return _client_has_intent(request)
    parsed_origin = urlparse(origin)
    parsed_host = urlparse(f"//{request.headers.get('Host', '')}")
    origin_host = (parsed_origin.hostname or "").lower()
    host = (parsed_host.hostname or "").lower()
    if not origin_host or not host or origin_host != host:
        return False
    request_scheme = str(request.url.scheme or "http").lower()
    if parsed_origin.scheme.lower() != request_scheme:
        return False
    default_port = 443 if request_scheme == "https" else 80
    try:
        return (parsed_origin.port or default_port) == (parsed_host.port or default_port)
    except ValueError:
        return False


def require_local_write_access(scope: str):
    """Require same-origin plus explicit client intent, without credentials."""

    async def dependency(request: Request):
        if not _request_is_same_origin(request):
            raise HTTPException(
                status_code=403,
                detail=f"Cross-origin request denied ({scope})",
            )
        if not _client_has_intent(request):
            raise HTTPException(
                status_code=403,
                detail=f"Client intent header required ({scope})",
            )

    return dependency
