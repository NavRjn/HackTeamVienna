"""Entry point for the local service. Always start it through this file:

    uv run python -m kraftsync.app.serve          # run
    uv run python -m kraftsync.app.serve --init   # only create token/port in .env and print them
    uv run python -m kraftsync.app.serve --reload # restart on changes to kraftsync/**/*.py and *.yaml (dev)

Safety on shared Wi-Fi:
  * binds to the LAN on purpose, but EVERY route except /health needs the X-KraftSync-Token header;
  * on first run it generates a strong token and a random port and stores them in .env (gitignored),
    so the URL stays stable across restarts;
  * refuses to start with a missing/short token; API docs are off; access log is off;
  * the token travels over plain HTTP: anyone sniffing the Wi-Fi can read it. For n8n Cloud use an
    https tunnel to this port instead of port-forwarding.
"""

import random
import secrets
import socket
import sys

import uvicorn
from dotenv import load_dotenv, set_key

from kraftsync.app.api import create_app
from kraftsync.app.settings import REPO_ROOT, MIN_TOKEN_LEN, Settings, SettingsError

ENV_FILE = REPO_ROOT / ".env"


def _free_port() -> int:
    rng = random.SystemRandom()
    while True:
        port = rng.randint(20000, 59999)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:   # nothing listening there
                return port


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def ensure_env() -> list[str]:
    """Create KRAFTSYNC_TOKEN / KRAFTSYNC_PORT in .env if missing. Returns what was generated."""
    import os
    ENV_FILE.touch(exist_ok=True)
    made = []
    if len(os.environ.get("KRAFTSYNC_TOKEN", "")) < MIN_TOKEN_LEN:
        os.environ["KRAFTSYNC_TOKEN"] = secrets.token_urlsafe(32)
        set_key(str(ENV_FILE), "KRAFTSYNC_TOKEN", os.environ["KRAFTSYNC_TOKEN"], quote_mode="never")
        made.append("token")
    if not os.environ.get("KRAFTSYNC_PORT"):
        os.environ["KRAFTSYNC_PORT"] = str(_free_port())
        set_key(str(ENV_FILE), "KRAFTSYNC_PORT", os.environ["KRAFTSYNC_PORT"], quote_mode="never")
        made.append("port")
    return made


def main() -> None:
    load_dotenv(ENV_FILE)
    made = ensure_env()
    try:
        settings = Settings.from_env()
        settings.validate()
    except SettingsError as exc:
        print(f"Refusing to start: {exc}", file=sys.stderr)
        sys.exit(2)

    url = f"http://{lan_ip()}:{settings.port}"
    print("=" * 64)
    print("KraftSync local service")
    print(f"  base URL : {url}   (health: {url}/health)")
    print(f"  header   : X-KraftSync-Token: {settings.token}")
    print(f"  live data: {settings.events_path.parent}  (events.jsonl, graph.json, kraftsync.log)")
    if made:
        print(f"  (generated {' + '.join(made)}, saved to .env)")
    if not settings.loopback:
        print("  bound to ALL interfaces: anyone on this network can reach it; the token is the only lock.")
    print("=" * 64)
    if "--init" in sys.argv:
        return
    common = dict(host=settings.host, port=settings.port, access_log=False, server_header=False)
    if "--reload" in sys.argv:
        # reload needs an import string; the factory re-reads the env (token/port already set above)
        uvicorn.run("kraftsync.app.api:create_app", factory=True, reload=True,
                    reload_dirs=[str(REPO_ROOT / "kraftsync")], reload_includes=["*.py", "*.yaml"], **common)
        return
    uvicorn.run(create_app(settings), **common)


if __name__ == "__main__":
    main()
