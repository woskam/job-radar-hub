"""
MCP server for Job Radar Hub -- exposes the same listing data as app.py's
REST API (via db/queries.py, shared so the two surfaces can't drift apart),
but over the Model Context Protocol instead of plain REST, for MCP-aware
agent clients.

Deliberately its own process (uvicorn/ASGI), not bolted onto the Flask/WSGI
app in app.py: the MCP SDK is built on Starlette, and running it as a
separate small process alongside gunicorn is simpler and more isolated than
trying to bridge WSGI and ASGI in one process.

Auth is hand-rolled rather than using the SDK's built-in OAuth-oriented
token_verifier/AuthSettings: this hub's auth model is a single static
bearer token looked up in api_keys, the same tokens the REST API already
uses -- there's no OAuth issuer/authorization server here, and forcing our
simple model through that machinery would mean standing up endpoints
(/.well-known/oauth-authorization-server, a token endpoint) that don't
apply to us. AuthMiddleware below checks the same api_keys table directly,
at the ASGI layer, mirroring exactly what app.py's Flask routes already do.
"""

import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from db.queries import lookup_api_key, query_listings

server = MCPServer(
    name="job-radar-hub",
    title="Job Radar Hub",
    description=(
        "Search generic job-listing data (title, company, location, url, description) fed by "
        "a Job Radar instance's own scrape cycle. No personal data -- no relevance scores, no "
        "application status, no letters/CVs, and no apply/send tool: this only ever returns "
        "listing data, what you do with it is up to you."
    ),
    website_url="https://job-radar-c66.pages.dev/",
)


@server.tool()
async def search_jobs(
    company: str | None = None,
    source: str | None = None,
    title_contains: str | None = None,
    location_contains: str | None = None,
    active: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Search job listings. All parameters are optional filters, combined with AND.

    company: exact company name match.
    source: exact source/ATS platform name match.
    title_contains: case-sensitive substring match on the job title.
    location_contains: case-sensitive substring match on the location.
    active: True (default) for currently-live listings, False for ones no longer
        seen in the source's latest scrape.
    limit: max results, default 50, capped at 200.
    offset: pagination offset, default 0.
    """
    return query_listings(
        company=company,
        source=source,
        title_contains=title_contains,
        location_contains=location_contains,
        active=active,
        limit=limit,
        offset=offset,
    )


# --- Auth + rate limiting (ASGI middleware, wraps the MCP app) --------------
# In-memory, single-process -- same real constraint as app.py's Flask-Limiter
# (see systemd/job-radar-hub.service's -w 1 comment): this process must stay
# single-instance for the rate limit to mean what it says. Tracked
# separately from the REST API's counter -- a key gets rate_limit_per_hour
# on each surface independently, not a combined budget. Acceptable
# simplification for v1; revisit with a shared store if that ever matters.
_recent_requests: dict[str, list[float]] = defaultdict(list)


def _rate_limited(key: str, limit_per_hour: int) -> bool:
    now = time.time()
    window_start = now - 3600
    hits = [t for t in _recent_requests[key] if t > window_start]
    hits.append(now)
    _recent_requests[key] = hits
    return len(hits) > limit_per_hour


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""

        row = lookup_api_key(token)
        if not row:
            response = JSONResponse({"error": "invalid or missing API key"}, status_code=401)
            return await response(scope, receive, send)
        if _rate_limited(token, row["rate_limit_per_hour"]):
            response = JSONResponse({"error": "rate limit exceeded"}, status_code=429)
            return await response(scope, receive, send)

        return await self.app(scope, receive, send)


def _transport_security():
    # The SDK's DNS-rebinding protection only allows "localhost" by default --
    # anything else (our real host, or later a real domain) gets a 421
    # Misdirected Request without this. MCP_ALLOWED_HOSTS is a comma-separated
    # list so the real deploy target (currently a bare IP:port, later a
    # domain) can be updated via .env without a code change.
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["localhost", "127.0.0.1"] + [
        h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=["*"])


def build_app():
    mcp_app = server.streamable_http_app(stateless_http=True, transport_security=_transport_security())
    return AuthMiddleware(mcp_app)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MCP_PORT", 5301)))
