# Job Radar Hub

A small, self-hostable, tokengated API that serves generic job-listing
data -- title, company, location, url, description -- fed by a push from
one or more [Job Radar](https://github.com/woskam/job-radar) instances'
own scrape cycles.

## What this is (and isn't)

Job Radar itself is a personal, self-hosted job-search tool: it scrapes
career pages across 100+ companies, scores matches against one person's
own profile, and drafts cover letters/CVs -- all of that stays private on
that person's own machine.

This hub is the generic, shareable half: the raw listing data, decoupled
from anyone's personal profile, scoring, or drafted documents. It exists
so that other tools or agents can query "what's currently open at these
companies" without each of them having to independently reverse-engineer
which ATS platform every company uses.

**This hub never sends anything to an employer, and never will.** It only
serves listing data out to whoever holds a valid API key. What a consumer
does with that data -- draft a letter, apply, ignore it -- is entirely
their own responsibility, on their own end.

## Ingest contract

A source instance pushes to `POST /ingest`, authenticated with
`Authorization: Bearer <HUB_PUSH_TOKEN>` (one shared secret -- this hub
assumes a single trusted producer per deployment, not a multi-tenant
ingest system). A push is a JSON object, not a bare array:

```json
{
  "scraped_ok": [
    {"source": "workday", "company": "Sample Sportswear Co"}
  ],
  "listings": [
    {
      "source": "workday",
      "external_id": "R00012345",
      "title": "Senior Digital Sales Manager",
      "company": "Sample Sportswear Co",
      "location": "Amsterdam, Netherlands",
      "url": "https://example.com/jobs/R00012345",
      "description": "...",
      "scraped_at": "2026-09-10T08:00:00+00:00"
    }
  ]
}
```

`scraped_ok` is which `(source, company)` pairs this push is authoritative
for -- required, and rejected with 400 if empty. `listings` only needs to
include what's still open for that coverage (may legitimately be `[]` if a
covered company currently has no open listings). A listing whose
`(source, company)` is in `scraped_ok` but that isn't in `listings` is
marked inactive (`active: 0`) and disappears from `GET /jobs`'s default
results; a listing whose company *isn't* in `scraped_ok` is left
untouched either way -- a push only ever speaks for what it actually
covers, never "everything not mentioned this time". This is deliberate:
if the source's scrape of a company failed or was skipped that cycle, it
simply won't appear in `scraped_ok`, and nothing of that company's gets
silently deactivated because of an outage on the producer's end.
Malformed listing entries (missing `source`/`external_id`) are skipped
individually rather than failing the whole batch.

Pushing is entirely optional from the source's side -- see
`job-radar/README.md`'s hub-integration section for how to opt a Job
Radar instance into pushing here.

## Read API

`GET /jobs`, authenticated with `Authorization: Bearer <api_key>` (issued
by hand, see below). Query params: `company`, `source`,
`title_contains`, `location_contains`, `active` (default `true`),
`limit` (default 50, max 200), `offset`. Rate-limited per key
(`rate_limit_per_hour`, set at issuance). Each result includes
`last_seen_at` -- this Hub's own receive time, updated whenever an ingest
touches that listing, so it's a genuine freshness signal (unlike
`scraped_at`, which is fixed at first-seen time and never moves).

`GET /health` -- no auth, liveness check.

## Email alerts

Visitors can save a search and get a daily digest email of new matching
listings -- the one place this hub stores real personal data (an email
address), so it's double opt-in and self-service end to end, unlike the
hand-issued `api_keys`.

- `GET`/`POST /alerts` -- a plain HTML signup form, served directly by
  this hub rather than from job-radar-site. That's deliberate, not an
  oversight: job-radar-site is HTTPS and this hub has no TLS yet (no
  domain to put a cert on), so a same-page JS submit from there would hit
  the browser's mixed-content blocking -- and routing it through a
  Cloudflare Pages Function relay doesn't work around that either,
  confirmed live: Cloudflare Workers' `fetch()` refuses any destination
  that resolves to a raw IP address outright (error 1003 "Direct IP
  Access Not Allowed"), even via a nip.io-style hostname trick, since
  Cloudflare inspects the resolved connection target, not the hostname
  string. A plain top-level link from job-radar-site to this same-origin
  form sidesteps both problems. Move the form back once this hub has a
  real domain + TLS.
- `POST /alerts/subscribe` -- the same thing as a JSON API instead of an
  HTML form, for any client that can reach this hub directly. Body:
  `{"email": "...", "keywords": [...], "exclude_keywords": [...],
  "location_mode": "remote" | "<city/country text>" | null,
  "category": "...", "segment": "...", "company": "..."}` (all filter
  fields optional). Keywords OR together and match against title+
  description; exclude_keywords NOT together the same way; category/
  segment match exactly (the same vocabulary as `companies.yaml`);
  company is a substring match. Always responds with the same generic
  "check your inbox" message regardless of whether the address is new,
  already subscribed, or the send failed -- this endpoint is intentionally
  not an email-existence oracle. Rate-limited per IP and, separately, per
  target email (max one confirm-send/hour to the same address -- the real
  abuse case is spamming a stranger's inbox, which per-IP limiting alone
  doesn't stop).
- `GET /alerts/confirm/<token>` -- the link in the confirm email. Sets the
  subscription active.
- `GET /alerts/unsubscribe/<token>` -- included in every digest email.

Matching happens on `scraped_at` (a listing's first-seen time, fixed once
and never touched again on a repeat sighting) -- not `last_seen_at`
(bumped on every ingest touch, including a still-open listing resent
unchanged by a producer's next scrape cycle).

`send_alerts.py` (run daily via `systemd/job-radar-hub-alerts.timer`)
matches each active subscriber against listings scraped since their last
digest, sends via [Resend](https://resend.com), and advances
`last_sent_at` -- only on a successful send, so a transient failure just
gets retried on the next day's run instead of silently dropping listings.
Also purges subscribers stuck in `pending` (never confirmed) after 7
days, to keep this hub's PII footprint small. See `.env.example` for
`RESEND_API_KEY`/`ALERTS_FROM_EMAIL`/`HUB_PUBLIC_BASE_URL`.

`manage.py list-subscribers` / `remove-subscriber <id-or-email>` for
admin/support use (a hard delete, unlike the self-service unsubscribe
link, which just flips `status`).

## MCP server

`mcp_server.py` exposes the same data over the Model Context Protocol
(Streamable HTTP transport) as a single `search_jobs` tool -- same filters
as `GET /jobs`, same API keys. Runs as its own process (default port
`5301`), separate from the Flask/gunicorn REST API: the MCP SDK is
Starlette/ASGI-based, so a second small process is simpler than bridging
WSGI and ASGI in one. Auth and rate limiting are hand-rolled
(`mcp_server.py::AuthMiddleware`) against the same `api_keys` table the
REST API uses, rather than the SDK's OAuth-oriented `token_verifier` --
there's no OAuth issuer here, just the same static bearer tokens.

```bash
./venv/bin/python mcp_server.py   # local dev, reads MCP_PORT from .env (default 5301)
```

The SDK's DNS-rebinding protection only allows `Host: localhost` by
default -- set `MCP_ALLOWED_HOSTS` in `.env` (comma-separated) to the
real host(s) this is served from, or every request gets a `421
Misdirected Request`.

Server card published at
[job-radar-c66.pages.dev/.well-known/mcp/server-card.json](https://job-radar-c66.pages.dev/.well-known/mcp/server-card.json)
(see the [job-radar-site](https://github.com/woskam/job-radar-site) repo).

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HUB_PUSH_TOKEN
./venv/bin/python app.py
```

Issue a consumer API key:

```bash
./venv/bin/python manage.py issue-key --owner "someone's agent" --rate-limit 100
```

## Deploying

`systemd/job-radar-hub.service` is a minimal long-running-service unit
(adjust `WorkingDirectory`/`ExecStart` to wherever you deploy this),
running the app through `gunicorn` rather than Flask's own dev server.
Where you actually host it is up to you; nothing here assumes a specific
provider -- but see the note below on why this needs a real persistent
disk, not a stateless/serverless platform, as-is.

### Google Cloud (Compute Engine)

Deliberately a plain VM, not Cloud Run: Cloud Run's filesystem doesn't
persist across restarts/scale-to-zero, which would silently wipe the
SQLite database (`listings`, `api_keys`) on every cold start. A small
Compute Engine VM (the Always Free tier's `e2-micro`, in `us-west1`/
`us-central1`/`us-east1`) has a real persistent disk, and runs this
exactly as tested locally -- no code changes needed.

```bash
# 1. Push this repo somewhere the VM can clone it (e.g. GitHub) first.

# 2. Create the VM -- deploy/gce-startup-script.sh installs deps, clones
#    the repo, and installs the systemd unit automatically on boot.
gcloud compute instances create job-radar-hub \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-type=pd-standard --boot-disk-size=10GB \
  --tags=job-radar-hub \
  --no-service-account --no-scopes \
  --metadata-from-file=startup-script=deploy/gce-startup-script.sh
# --boot-disk-type=pd-standard matters: GCP's current default is pd-balanced,
# which is NOT covered by the Always Free tier (small but real ongoing cost).
# No --address flag -- an ephemeral (not static/reserved) external IP is
# free while the VM is running; a reserved static IP costs money even when
# unattached, and there's no need for one until a real domain points here.
# --no-service-account --no-scopes: this VM never calls any GCP API itself
# (it only talks to its own SQLite db and answers HTTP requests), so it
# doesn't need one attached -- also sidesteps needing the deploying
# identity to hold iam.serviceAccountUser on the project's default service
# account, which a scoped-down deployer (e.g. just Compute Admin) won't
# have by default.

# 3. Open the firewall for both ports -- 5300 (REST API), 5301 (MCP server).
gcloud compute firewall-rules create allow-job-radar-hub \
  --allow=tcp:5300 --target-tags=job-radar-hub \
  --description="Job Radar Hub API"
gcloud compute firewall-rules create allow-job-radar-hub-mcp \
  --allow=tcp:5301 --target-tags=job-radar-hub \
  --description="Job Radar Hub MCP server"

# 4. SSH in once to create .env by hand -- never baked into the startup
#    script or committed (same convention as job-radar's own .env).
gcloud compute ssh job-radar-hub --zone=us-central1-a
  sudo su -
  cd /opt/job-radar-hub
  cp .env.example .env
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into HUB_PUSH_TOKEN=
  nano .env   # also set MCP_ALLOWED_HOSTS=<this VM's IP>:5301 (see "MCP server" above)
  systemctl restart job-radar-hub job-radar-hub-mcp

# 5. Confirm it's reachable.
IP=$(gcloud compute instances describe job-radar-hub --zone=us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
curl "http://$IP:5300/health"
```

No custom domain/TLS yet by design (plain HTTP on the raw port, fine for
initial testing) -- once a domain is pointed at this VM, put Caddy or
Cloudflare in front for HTTPS rather than terminating TLS in the app
itself.

## Not in v1

No payment/billing integration and no public self-serve signup for
*read-API keys* -- those are still issued by hand via `manage.py` (email
alert subscriptions are self-service, see above, but that's a narrower
capability than an API key). No admin UI. These are intentionally
deferred until there's real demand to justify them.
