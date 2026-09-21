#!/usr/bin/env python3
"""
CLI to manage consumer API keys -- no self-serve signup in v1, keys are
issued by hand.

Only sha256(key) is ever stored -- a leaked database file is then not a
leak of anyone's actual credential. issue-key is the only time the raw key
is ever shown; there's no way to recover it afterward, only reissue a new
one. 24 random bytes is plenty of entropy that a plain (fast) SHA-256 is
fine here -- this isn't a low-entropy user password that needs a slow hash
to resist brute-forcing.

Usage:
    python manage.py issue-key --owner "some agent/person" [--rate-limit 100]
    python manage.py revoke-key <id-or-key>
    python manage.py list-keys
"""

import argparse
import hashlib
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import get_db


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue_key(owner: str, rate_limit: int) -> None:
    key = f"jrh_{secrets.token_urlsafe(24)}"
    preview = key[:12] + "..."
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (key, key_preview, owner, rate_limit_per_hour, created_at) VALUES (?, ?, ?, ?, ?)",
        (_hash(key), preview, owner, rate_limit, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print(f"Issued key for {owner!r} ({rate_limit}/hour) -- shown once, save it now:\n{key}")


def revoke_key(id_or_key: str) -> None:
    conn = get_db()
    if id_or_key.isdigit():
        cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (int(id_or_key),))
    else:
        cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE key = ?", (_hash(id_or_key),))
    conn.commit()
    conn.close()
    print("Revoked." if cur.rowcount else "No such key.")


def list_keys() -> None:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, key_preview, owner, rate_limit_per_hour, revoked, created_at FROM api_keys ORDER BY id"
    ).fetchall()
    conn.close()
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        preview = row["key_preview"] or "(issued before key_preview existed)"
        print(f"#{row['id']}  {preview}  {row['owner']!r}  {row['rate_limit_per_hour']}/hour  [{status}]  {row['created_at']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue-key")
    issue.add_argument("--owner", required=True)
    issue.add_argument("--rate-limit", type=int, default=100)

    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("id_or_key", help="The numeric id from list-keys, or the raw key if you still have it")

    sub.add_parser("list-keys")

    args = parser.parse_args()
    if args.command == "issue-key":
        issue_key(args.owner, args.rate_limit)
    elif args.command == "revoke-key":
        revoke_key(args.id_or_key)
    elif args.command == "list-keys":
        list_keys()


if __name__ == "__main__":
    main()
