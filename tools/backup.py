"""Back up all your Daybook data to one JSON file on your computer, or restore it.

Usage (on your Mac, in Terminal, from the project folder):
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="sb_secret_..."
    python3 tools/backup.py                  # writes daybook-backup-YYYY-MM-DD.json
    python3 tools/backup.py --restore FILE   # puts a backup back into the database

The file has the same format as the app's Export backup button (and the
claude.ai Daybook's), so the app's Import backup button reads it too.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

import requests


def client() -> tuple[str, dict]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY first (see the setup guide).")
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # legacy service_role key
        headers["Authorization"] = f"Bearer {key}"
    return url.rstrip("/") + "/rest/v1", headers


def fetch_all(base: str, headers: dict) -> list[dict]:
    rows, start = [], 0
    while True:
        r = requests.get(f"{base}/docs", headers={**headers, "Range": f"{start}-{start + 999}"},
                         params={"select": "user_id,col,id,data"}, timeout=60)
        r.raise_for_status()
        page = r.json()
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def backup(path: str | None) -> str:
    base, headers = client()
    rows = fetch_all(base, headers)
    out: dict = {"app": "daybook", "version": 2, "exportedAt": date.today().isoformat(),
                 "userIds": sorted({r["user_id"] for r in rows})}
    for r in rows:
        out.setdefault(r["col"], {})[r["id"]] = r["data"]
    path = path or f"daybook-backup-{date.today().isoformat()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    counts = ", ".join(f"{len(v)} {k}" for k, v in out.items() if isinstance(v, dict))
    print(f"Saved {path} ({counts or 'no data yet'})")
    return path


def restore(path: str, user_id: str | None) -> int:
    base, headers = client()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    uid = user_id or (data.get("userIds") or [None])[0]
    if not uid:
        sys.exit("Add --user-id (find it in Supabase under Authentication > Users).")
    stamp = datetime.now(timezone.utc).isoformat()
    rows = [{"user_id": uid, "col": col, "id": doc_id, "data": doc, "updated_at": stamp}
            for col, docs in data.items() if isinstance(docs, dict)
            for doc_id, doc in docs.items()]
    for i in range(0, len(rows), 200):
        r = requests.post(f"{base}/docs", headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          params={"on_conflict": "user_id,col,id"}, json=rows[i:i + 200], timeout=60)
        r.raise_for_status()
    print(f"Restored {len(rows)} items")
    return len(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Back up or restore Daybook data.")
    p.add_argument("--out", help="backup file name")
    p.add_argument("--restore", metavar="FILE", help="restore from this backup file")
    p.add_argument("--user-id", help="account to restore into (defaults to the one in the file)")
    a = p.parse_args()
    restore(a.restore, a.user_id) if a.restore else backup(a.out)
