#!/usr/bin/env python3
"""
CLI to manage consumer API keys -- no self-serve signup in v1, keys are
issued by hand.

Usage:
    python manage.py issue-key --owner "some agent/person" [--rate-limit 100]
    python manage.py revoke-key <key>
    python manage.py list-keys
"""

import argparse
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import get_db


def issue_key(owner: str, rate_limit: int) -> None:
    key = f"jrh_{secrets.token_urlsafe(24)}"
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (key, owner, rate_limit_per_hour, created_at) VALUES (?, ?, ?, ?)",
        (key, owner, rate_limit, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print(f"Issued key for {owner!r} ({rate_limit}/hour):\n{key}")


def revoke_key(key: str) -> None:
    conn = get_db()
    cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    print("Revoked." if cur.rowcount else "No such key.")


def list_keys() -> None:
    conn = get_db()
    rows = conn.execute("SELECT key, owner, rate_limit_per_hour, revoked, created_at FROM api_keys").fetchall()
    conn.close()
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        print(f"{row['key']}  {row['owner']!r}  {row['rate_limit_per_hour']}/hour  [{status}]  {row['created_at']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue-key")
    issue.add_argument("--owner", required=True)
    issue.add_argument("--rate-limit", type=int, default=100)

    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("key")

    sub.add_parser("list-keys")

    args = parser.parse_args()
    if args.command == "issue-key":
        issue_key(args.owner, args.rate_limit)
    elif args.command == "revoke-key":
        revoke_key(args.key)
    elif args.command == "list-keys":
        list_keys()


if __name__ == "__main__":
    main()
