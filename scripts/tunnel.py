"""HTTPS tunnel from the local KraftSync service to the internet, for n8n Cloud.

    uv run python scripts/tunnel.py                 # quick tunnel: random https://<x>.trycloudflare.com
    uv run python scripts/tunnel.py --show-token    # also print the token (not on a projector!)

Start the service first (`uv run python -m kraftsync.app.serve`), then this in a second terminal and
leave it open. It:
  1. finds cloudflared (PATH, the default Windows install folder, or CLOUDFLARED=<path>),
  2. checks the service answers locally on KRAFTSYNC_PORT,
  3. starts the tunnel and reads its public https URL,
  4. checks through the tunnel: /health is ok, /state WITHOUT the token is refused (401) and WITH the
     token works - so the lock holds before anyone uses the URL,
  5. prints what to set in n8n and saves the URL as KRAFTSYNC_PUBLIC_URL in .env.
Ctrl+C stops the tunnel.

A quick tunnel gets a NEW url every start: update n8n's KRAFTSYNC_BASE_URL each time. For a stable URL
use a named tunnel (free Cloudflare account + a domain on Cloudflare), once:
    cloudflared tunnel login
    cloudflared tunnel create kraftsync
    cloudflared tunnel route dns kraftsync kraftsync.<your-domain>
then set in .env:  CLOUDFLARED_TUNNEL_NAME=kraftsync  and  KRAFTSYNC_PUBLIC_URL=https://kraftsync.<your-domain>
and this script runs the named tunnel instead (same checks, the URL never changes).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
WINDOWS_PATHS = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")
TOKEN_HEADER = "X-KraftSync-Token"


def find_cloudflared() -> str | None:
    for candidate in (os.environ.get("CLOUDFLARED"), shutil.which("cloudflared"), *WINDOWS_PATHS):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]} ({len(token)} chars, full value in .env)" if len(token) > 12 else "***"


def parse_quick_url(line: str) -> str | None:
    m = QUICK_URL.search(line)
    return m.group(0) if m and "api.trycloudflare.com" not in m.group(0) else None


def check(base: str, token: str, timeout_s: float = 90) -> list[str]:
    """Through the tunnel: health ok, no token -> 401, token -> 200. Returns problems ([] = ok).
    Retries while the new hostname propagates (usually a few seconds)."""
    deadline, last = time.monotonic() + timeout_s, "no answer yet"
    with httpx.Client(timeout=10, follow_redirects=False) as c:
        while time.monotonic() < deadline:
            try:
                r = c.get(f"{base}/health")
                if r.status_code == 200 and r.json().get("ok"):
                    break
                last = f"/health answered {r.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                last = f"{e.__class__.__name__}"
            time.sleep(2)
        else:
            return [f"the tunnel does not reach the service ({last})"]
        problems = []
        no_token = c.get(f"{base}/state").status_code
        if no_token != 401:
            problems.append(f"/state WITHOUT the token answered {no_token}, expected 401 - do not share this URL")
        with_token = c.get(f"{base}/state", headers={TOKEN_HEADER: token}).status_code
        if with_token != 200:
            problems.append(f"/state WITH the token answered {with_token}, expected 200")
        return problems


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)   # visible when piped/logged
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show-token", action="store_true", help="print the full token (keep it off screen recordings)")
    a = ap.parse_args(argv)

    load_dotenv(ENV_FILE)
    port, token = os.environ.get("KRAFTSYNC_PORT", ""), os.environ.get("KRAFTSYNC_TOKEN", "")
    if not port or len(token) < 24:
        print("No KRAFTSYNC_PORT / KRAFTSYNC_TOKEN in .env. Run: uv run python -m kraftsync.app.serve --init")
        return 2
    exe = find_cloudflared()
    if not exe:
        print("cloudflared not found. Install it:  winget install --id Cloudflare.cloudflared   (or set CLOUDFLARED=<path>)")
        return 2
    local = f"http://127.0.0.1:{port}"
    try:
        httpx.get(f"{local}/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        print(f"The service is not answering on {local}. Start it first:  uv run python -m kraftsync.app.serve")
        return 2

    name, fixed_url = os.environ.get("CLOUDFLARED_TUNNEL_NAME"), os.environ.get("KRAFTSYNC_PUBLIC_URL")
    named = bool(name and fixed_url and "trycloudflare.com" not in fixed_url)
    cmd = ([exe, "tunnel", "--no-autoupdate", "run", "--url", local, name] if named
           else [exe, "tunnel", "--no-autoupdate", "--url", local])
    print(f"starting {'named tunnel ' + name if named else 'quick tunnel'} -> {local} …")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    found: dict[str, str] = {}
    connected = threading.Event()

    def pump() -> None:                      # cloudflared logs to stderr; keep reading so it never blocks
        for line in proc.stdout:
            if not named and "url" not in found and (u := parse_quick_url(line)):
                found["url"] = u
            if "Registered tunnel connection" in line:
                connected.set()
            if " ERR " in line:
                print(f"  cloudflared: {line.strip()[:200]}")

    threading.Thread(target=pump, daemon=True).start()
    try:
        url = fixed_url if named else None
        deadline = time.monotonic() + 60
        while url is None and time.monotonic() < deadline and proc.poll() is None:
            url = found.get("url")
            time.sleep(0.3)
        if url is None:
            print("cloudflared did not report a URL (is a ~/.cloudflared/config.yml interfering, or no internet?)")
            return 1
        connected.wait(30)
        url = url.rstrip("/")
        print(f"tunnel up: {url}\nchecking through the tunnel …")
        problems = check(url, token)
        if problems:
            for p in problems:
                print(f"  PROBLEM  {p}")
            return 1
        set_key(str(ENV_FILE), "KRAFTSYNC_PUBLIC_URL", url, quote_mode="never")
        print("=" * 72)
        print("OK: /health reachable, /state refused without token (401), works with token (200)")
        print("In n8n (Cloud) set:")
        print(f"  KRAFTSYNC_BASE_URL = {url}")
        print(f"  header {TOKEN_HEADER} = {token if a.show_token else mask(token)}")
        print(f"(also saved as KRAFTSYNC_PUBLIC_URL in .env){'' if named else '  - quick tunnel: new URL on every restart'}")
        print("Leave this window open. Ctrl+C stops the tunnel.")
        print("=" * 72)
        return proc.wait()
    except KeyboardInterrupt:
        print("\nstopping tunnel …")
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
