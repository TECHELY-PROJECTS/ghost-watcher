#!/usr/bin/env python3
"""
ghost_watch.py — ultra-aggressive GitHub repo "goes public" watcher + instant capture.

Polls a repo's public URL on a sub-second cadence. The moment it returns 200,
it fires EVERY capture method in parallel (mirror clone, tarball, zipball, API
metadata) so that even a 40-second public window is enough to get everything.

Usage:
    python3 ghost_watch.py                          # defaults below
    python3 ghost_watch.py --repo owner/name --interval 0.5 --out ./capture
    GITHUB_TOKEN=ghp_xxx python3 ghost_watch.py     # optional, raises API limits

No third-party dependencies. Python 3.7+.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_REPO = "mrhakash/ghost-ai-writer-v2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# NB: a bot-ish UA gets HTTP 403 throttled by GitHub within ~60 polls from
# datacenter IPs. A normal browser UA polls cleanly. Measured, not assumed.


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(msg, logfile=None):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def http(url, method="GET", token=None, timeout=8, accept=None):
    """Return (status, headers, body_bytes). Never raises on HTTP error codes."""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", accept or "*/*")
    req.add_header("Cache-Control", "no-cache")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), (r.read() if method == "GET" else b"")
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, dict(e.headers or {}), body
    except Exception as e:
        return 0, {"_error": str(e)}, b""


# ----------------------------- capture actions ------------------------------

def run(cmd, cwd, logfile, label, timeout=900):
    log(f"CAPTURE[{label}] start: {' '.join(cmd)}", logfile)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        ok = p.returncode == 0
        log(f"CAPTURE[{label}] {'OK' if ok else 'FAIL rc=' + str(p.returncode)}", logfile)
        if not ok:
            log(f"CAPTURE[{label}] stderr: {p.stderr.decode('utf-8', 'replace')[:800]}", logfile)
        return ok
    except subprocess.TimeoutExpired:
        log(f"CAPTURE[{label}] TIMEOUT", logfile)
        return False
    except Exception as e:
        log(f"CAPTURE[{label}] ERROR {e}", logfile)
        return False


def fetch_to_file(url, dest, token, logfile, label, retries=4):
    for attempt in range(1, retries + 1):
        status, hdrs, body = http(url, token=token, timeout=180)
        if status == 200 and body:
            with open(dest, "wb") as f:
                f.write(body)
            log(f"CAPTURE[{label}] OK {len(body)} bytes -> {os.path.basename(dest)}", logfile)
            return True
        log(f"CAPTURE[{label}] attempt {attempt} status={status} {hdrs.get('_error','')}", logfile)
        time.sleep(0.4 * attempt)
    return False


def api_dump(repo, outdir, token, logfile):
    endpoints = {
        "repo.json": f"https://api.github.com/repos/{repo}",
        "branches.json": f"https://api.github.com/repos/{repo}/branches?per_page=100",
        "tags.json": f"https://api.github.com/repos/{repo}/tags?per_page=100",
        "releases.json": f"https://api.github.com/repos/{repo}/releases?per_page=100",
        "commits.json": f"https://api.github.com/repos/{repo}/commits?per_page=100",
        "issues.json": f"https://api.github.com/repos/{repo}/issues?state=all&per_page=100",
        "pulls.json": f"https://api.github.com/repos/{repo}/pulls?state=all&per_page=100",
        "contributors.json": f"https://api.github.com/repos/{repo}/contributors?per_page=100",
    }
    meta = os.path.join(outdir, "metadata")
    os.makedirs(meta, exist_ok=True)
    for name, url in endpoints.items():
        status, _, body = http(url, token=token, timeout=30,
                               accept="application/vnd.github+json")
        if status == 200:
            with open(os.path.join(meta, name), "wb") as f:
                f.write(body)
    log("CAPTURE[api] metadata dump complete", logfile)


def capture(repo, outdir, token, logfile):
    """Fire every capture path in parallel. Designed to finish well inside 40s."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(outdir, f"{repo.split('/')[-1]}-{stamp}")
    os.makedirs(dest, exist_ok=True)
    log(f"*** REPO IS PUBLIC — capturing into {dest} ***", logfile)

    url_https = f"https://github.com/{repo}.git"
    if token:
        url_https = f"https://x-access-token:{token}@github.com/{repo}.git"

    jobs = [
        # Full history, all branches + tags. This is the one that matters most.
        threading.Thread(target=run, args=(
            ["git", "clone", "--mirror", url_https, "mirror.git"],
            dest, logfile, "git-mirror")),
        # Fast flat snapshot of the default branch — usually lands in 1-3s.
        threading.Thread(target=fetch_to_file, args=(
            f"https://api.github.com/repos/{repo}/tarball", os.path.join(dest, "snapshot.tar.gz"),
            token, logfile, "tarball")),
        threading.Thread(target=fetch_to_file, args=(
            f"https://api.github.com/repos/{repo}/zipball", os.path.join(dest, "snapshot.zip"),
            token, logfile, "zipball")),
        # Codeload direct paths, in case the API is throttled.
        threading.Thread(target=fetch_to_file, args=(
            f"https://codeload.github.com/{repo}/tar.gz/refs/heads/main",
            os.path.join(dest, "main.tar.gz"), token, logfile, "codeload-main")),
        threading.Thread(target=fetch_to_file, args=(
            f"https://codeload.github.com/{repo}/tar.gz/refs/heads/master",
            os.path.join(dest, "master.tar.gz"), token, logfile, "codeload-master")),
        threading.Thread(target=api_dump, args=(repo, dest, token, logfile)),
    ]
    for j in jobs:
        j.daemon = True
        j.start()
    for j in jobs:
        j.join(timeout=900)

    # Materialise a normal working checkout from the mirror (offline, safe).
    mirror = os.path.join(dest, "mirror.git")
    if os.path.isdir(mirror):
        run(["git", "clone", mirror, "worktree"], dest, logfile, "worktree")

    # Release assets, if any.
    rel = os.path.join(dest, "metadata", "releases.json")
    if os.path.exists(rel):
        try:
            rels = json.load(open(rel))
            adir = os.path.join(dest, "release-assets")
            for r in rels:
                for a in r.get("assets", []):
                    os.makedirs(adir, exist_ok=True)
                    fetch_to_file(a["browser_download_url"],
                                  os.path.join(adir, a["name"]), token, logfile,
                                  f"asset:{a['name']}")
        except Exception as e:
            log(f"CAPTURE[assets] skipped: {e}", logfile)

    with open(os.path.join(dest, "CAPTURED_AT.txt"), "w") as f:
        f.write(f"{ts()}\nrepo: {repo}\n")
    log(f"*** CAPTURE COMPLETE -> {dest} ***", logfile)
    return dest


# -------------------------------- watch loop --------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "capture"))
    ap.add_argument("--once", action="store_true", help="single check, then exit")
    ap.add_argument("--max-runtime", type=float, default=0,
                    help="stop after N seconds (0 = forever). For CI job ceilings.")
    ap.add_argument("--keep-watching", action="store_true",
                    help="keep polling after a successful capture (catch later re-publishes)")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    os.makedirs(args.out, exist_ok=True)
    logfile = os.path.join(args.out, "watch.log")

    log(f"Watching https://github.com/{args.repo} every {args.interval}s "
        f"(token: {'yes' if token else 'no'}) -> {args.out}", logfile)

    probe = f"https://github.com/{args.repo}"
    n = 0
    backoff = 0.0
    last_heartbeat = 0.0
    started = time.time()

    def emit_output(key, value):
        """Expose a result to GitHub Actions, if we're running inside it."""
        gho = os.environ.get("GITHUB_OUTPUT")
        if gho:
            try:
                with open(gho, "a") as f:
                    f.write(f"{key}={value}\n")
            except Exception:
                pass

    while True:
        if args.max_runtime and (time.time() - started) > args.max_runtime:
            log(f"max-runtime reached after {n} polls — exiting cleanly "
                f"(an overlapping job should still be watching)", logfile)
            emit_output("captured", "false")
            return 0
        n += 1
        status, hdrs, _ = http(probe, method="HEAD", timeout=6)

        if status == 200:
            log(f"poll #{n}: 200 — PUBLIC DETECTED", logfile)
            try:
                capture(args.repo, args.out, token, logfile)
            except Exception as e:
                log(f"capture crashed: {e}", logfile)
            emit_output("captured", "true")
            if not args.keep_watching:
                return 0
            time.sleep(30)  # don't re-capture the same window
            continue

        if status == 429 or status == 403:
            backoff = min((backoff or args.interval) * 2, 30)
            log(f"poll #{n}: {status} rate-limited, backing off {backoff:.1f}s", logfile)
            time.sleep(backoff)
            continue
        backoff = 0.0

        if status not in (404, 0):
            log(f"poll #{n}: unexpected status {status}", logfile)
        if status == 0:
            log(f"poll #{n}: network error {hdrs.get('_error','')}", logfile)

        now = time.time()
        if now - last_heartbeat > 300:
            log(f"still watching — {n} polls, last status {status}", logfile)
            last_heartbeat = now

        if args.once:
            print(f"status={status}")
            return 0

        # small jitter so we don't sync into a predictable pattern
        time.sleep(max(0.2, args.interval + random.uniform(-0.15, 0.15)))


if __name__ == "__main__":
    sys.exit(main())
