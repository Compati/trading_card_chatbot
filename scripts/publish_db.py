"""Publish db/cards.db as the 'cards.db' asset on the rolling 'db-latest' release.

The DB is not committed to git (it approaches GitHub's 100 MB single-file limit and
bloats history). Instead each build is uploaded as a Release asset (up to 2 GB,
free, outside the LFS quota) and pulled by scripts/fetch_db.sh on deploy.

Run after a data update:  .venv\\Scripts\\python.exe scripts\\publish_db.py

Auth token is resolved from, in order: $GITHUB_TOKEN / $GH_TOKEN, `gh auth token`,
then the git credential helper (the same login your `git push` already uses).
Needs a token with 'repo' scope (or 'public_repo' for this public repo).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "db" / "cards.db"
TAG = "db-latest"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def repo_slug() -> str:
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()
    slug = url.split("github.com")[-1].lstrip(":/")
    return slug[:-4] if slug.endswith(".git") else slug


def resolve_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    for cmd in (["gh", "auth", "token"],):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except FileNotFoundError:
            pass
    try:  # git credential helper (same login as `git push`)
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=ROOT, capture_output=True, text=True,
        )
        for line in out.stdout.splitlines():
            if line.startswith("password="):
                return line[len("password="):]
    except Exception:
        pass
    sys.exit("No GitHub token found. Set GITHUB_TOKEN, or run `gh auth login`, "
             "or ensure your git credential helper has a github.com login.")


def api(method: str, url: str, token: str, data: bytes | None = None,
        content_type: str = "application/json") -> dict:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"GitHub API {method} {url} -> {e.code}\n{detail}")


def main() -> None:
    if not DB.exists():
        sys.exit(f"missing {DB}")
    slug = repo_slug()
    token = resolve_token()
    size_mb = DB.stat().st_size / 1e6
    print(f"repo={slug}  db={DB.name} ({size_mb:.1f} MB)  tag={TAG}")

    # Get or create the rolling release.
    try:
        rel = api("GET", f"{API}/repos/{slug}/releases/tags/{TAG}", token)
    except SystemExit:
        rel = None
    if not rel or "id" not in rel:
        rel = api("POST", f"{API}/repos/{slug}/releases", token,
                  json.dumps({"tag_name": TAG, "name": "Latest DB build",
                              "body": "Rolling latest cards.db. Updated by scripts/publish_db.py."}).encode())
        print(f"created release {TAG}")
    rid = rel["id"]

    # Replace any existing cards.db asset.
    for asset in api("GET", f"{API}/repos/{slug}/releases/{rid}/assets", token):
        if asset.get("name") == "cards.db":
            api("DELETE", f"{API}/repos/{slug}/releases/assets/{asset['id']}", token)
            print("removed previous asset")

    print("uploading… (this sends the whole DB, may take a bit)")
    api("POST", f"{UPLOADS}/repos/{slug}/releases/{rid}/assets?name=cards.db",
        token, data=DB.read_bytes(), content_type="application/octet-stream")
    print(f"✓ published cards.db to release '{TAG}'")
    print(f"  download URL: https://github.com/{slug}/releases/download/{TAG}/cards.db")


if __name__ == "__main__":
    main()
