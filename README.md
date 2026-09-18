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

A source instance pushes its full current snapshot of listings as a JSON
array to `POST /ingest`, authenticated with `Authorization: Bearer
<HUB_PUSH_TOKEN>` (one shared secret -- this hub assumes a single trusted
producer per deployment, not a multi-tenant ingest system). Each item:

```json
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
```

Every push is treated as authoritative for "what's live right now":
anything not included in a push is marked inactive (`active: 0`) and
disappears from `GET /jobs`'s default results, no separate diff/removal
step needed. Malformed entries (missing `source`/`external_id`) are
skipped individually rather than failing the whole batch.

Pushing is entirely optional from the source's side -- see
`job-radar/README.md`'s hub-integration section for how to opt a Job
Radar instance into pushing here.

## Read API

`GET /jobs`, authenticated with `Authorization: Bearer <api_key>` (issued
by hand, see below). Query params: `company`, `source`,
`title_contains`, `location_contains`, `active` (default `true`),
`limit` (default 50, max 200), `offset`. Rate-limited per key
(`rate_limit_per_hour`, set at issuance).

`GET /health` -- no auth, liveness check.

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

# 3. Open the firewall for the app's port (5300).
gcloud compute firewall-rules create allow-job-radar-hub \
  --allow=tcp:5300 --target-tags=job-radar-hub \
  --description="Job Radar Hub API"

# 4. SSH in once to create .env by hand -- never baked into the startup
#    script or committed (same convention as job-radar's own .env).
gcloud compute ssh job-radar-hub --zone=us-central1-a
  sudo su -
  cd /opt/job-radar-hub
  cp .env.example .env
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into HUB_PUSH_TOKEN=
  nano .env
  systemctl restart job-radar-hub

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

No payment/billing integration and no public self-serve signup --
consumer keys are issued by hand via `manage.py`. No admin UI. These are
intentionally deferred until there's real demand to justify them.
