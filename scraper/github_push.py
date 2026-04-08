"""
github_push.py — reusable GitHub file pusher using GITHUB_PERSONAL_ACCESS_TOKEN.

Usage from another script:
    from github_push import push_files
    push_files([
        (".github/workflows/scrape.yml", "/path/to/local/scrape.yml"),
        ("dashboard/records.json",       "/path/to/local/records.json"),
    ], message="chore: update data")

Or from command line:
    python scraper/github_push.py dashboard/records.json data/leads.csv
    (pushes local workspace copies of those files)
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
REPO     = "sailakshmi01/richmond-city-va"
API      = "https://api.github.com"


def _get_token() -> str:
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GITHUB_PERSONAL_ACCESS_TOKEN secret not set. "
            "Add it in Replit Secrets with 'repo' + 'workflow' scopes."
        )
    return token


def _gh_request(path: str, method: str = "GET", body: dict = None, token: str = None):
    url  = f"{API}/repos/{REPO}/contents/{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "replit-scraper-push",
        "Content-Type":  "application/json",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _get_sha(path: str, token: str) -> str | None:
    d, status = _gh_request(path, token=token)
    return d.get("sha") if status == 200 else None


def push_files(
    file_pairs: list[tuple[str, str]],
    message: str = "chore: automated push from Replit",
    verbose: bool = True,
) -> dict[str, bool]:
    """
    Push a list of (remote_path, local_path) pairs to GitHub.
    Returns a dict of {remote_path: success}.
    """
    token   = _get_token()
    results = {}

    for remote_path, local_path in file_pairs:
        try:
            with open(local_path, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            sha  = _get_sha(remote_path, token)
            body = {"message": message, "content": content}
            if sha:
                body["sha"] = sha
            d, status = _gh_request(remote_path, "PUT", body, token)
            ok = status in (200, 201)
            results[remote_path] = ok
            if verbose:
                sha_short = (d.get("content") or {}).get("sha", "")[:7]
                symbol = "✅" if ok else "❌"
                detail = sha_short if ok else d.get("message", str(status))
                print(f"  {symbol} {remote_path} → {detail}")
        except Exception as e:
            results[remote_path] = False
            if verbose:
                print(f"  ❌ {remote_path}: {e}")

    return results


def push_standard_outputs(message: str = None) -> None:
    """Push the standard scraper outputs: records.json (×2) + leads.csv."""
    if not message:
        from datetime import datetime
        message = f"chore: scraper output — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    push_files([
        ("dashboard/records.json",  str(BASE_DIR / "dashboard" / "records.json")),
        ("data/records.json",       str(BASE_DIR / "data" / "records.json")),
        ("data/leads.csv",          str(BASE_DIR / "data" / "leads.csv")),
    ], message=message)


def push_all(message: str = None) -> None:
    """Push every tracked file: workflows + scraper + outputs."""
    if not message:
        from datetime import datetime
        message = f"chore: full push — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    push_files([
        (".github/workflows/scrape.yml",    str(BASE_DIR / ".github/workflows/scrape.yml")),
        (".github/workflows/test-ocis.yml", str(BASE_DIR / ".github/workflows/test-ocis.yml")),
        ("scraper/fetch.py",                str(BASE_DIR / "scraper/fetch.py")),
        ("scraper/test_ocis.py",            str(BASE_DIR / "scraper/test_ocis.py")),
        ("scraper/requirements.txt",        str(BASE_DIR / "scraper/requirements.txt")),
        ("scraper/github_push.py",          str(BASE_DIR / "scraper/github_push.py")),
        ("dashboard/records.json",          str(BASE_DIR / "dashboard/records.json")),
        ("data/records.json",               str(BASE_DIR / "data/records.json")),
        ("data/leads.csv",                  str(BASE_DIR / "data/leads.csv")),
    ], message=message)


# ---------------------------------------------------------------------------
# CLI usage: python scraper/github_push.py [file1 file2 ...]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scraper/github_push.py [all | outputs | remote/path ...]")
        print("  all     — push every tracked file")
        print("  outputs — push records.json + leads.csv only")
        sys.exit(0)

    arg = sys.argv[1]
    if arg == "all":
        print("Pushing all tracked files…")
        push_all()
    elif arg == "outputs":
        print("Pushing scraper outputs…")
        push_standard_outputs()
    else:
        pairs = []
        for remote_path in sys.argv[1:]:
            local_path = str(BASE_DIR / remote_path)
            pairs.append((remote_path, local_path))
        print(f"Pushing {len(pairs)} file(s)…")
        push_files(pairs)

    print("Done.")
