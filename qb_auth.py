"""QuickBooks Online OAuth 2.0: one-time login and token storage.

    python qb_auth.py connect            # opens the browser, you click Allow, tokens are saved
    python qb_auth.py connect --manual   # if the local callback server does not work: paste the redirect URL
    python qb_auth.py status             # shows what is saved (never prints the tokens)

How it works (Authorization Code flow):
  1. We send you to Intuit with our client id, the scope and a random `state`.
  2. You log in and click Allow. Intuit redirects the browser to QB_REDIRECT_URI with ?code=...&realmId=...&state=...
     (realmId is the id of the QuickBooks company you connected.)
  3. We check `state`, then exchange the code for an access token (about 1 hour) and a refresh token.
  4. The refresh token ROTATES: every refresh returns a new one and the old one stops working.
     refresh_tokens() therefore saves the new token immediately, before anything else.

Tokens live in .qb_tokens.json, which is in .gitignore. Never commit it.
"""
import json
import os
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TOKEN_FILE = ".qb_tokens.json"
ENVIRONMENTS = ("sandbox", "production")


class QBAuthError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config(env=None):
    """Read QB_* settings from the environment (.env is loaded when `env` is not given)."""
    if env is None:
        from dotenv import load_dotenv
        load_dotenv()
        env = os.environ
    missing = [k for k in ("QB_CLIENT_ID", "QB_CLIENT_SECRET", "QB_REDIRECT_URI") if not env.get(k)]
    if missing:
        raise QBAuthError("missing in .env: " + ", ".join(missing))
    environment = env.get("QB_ENVIRONMENT", "sandbox").strip().lower()
    if environment not in ENVIRONMENTS:
        raise QBAuthError(f"QB_ENVIRONMENT must be one of {ENVIRONMENTS}, got {environment!r}")
    return {"client_id": env["QB_CLIENT_ID"].strip(), "client_secret": env["QB_CLIENT_SECRET"].strip(),
            "redirect_uri": env["QB_REDIRECT_URI"].strip(), "environment": environment,
            "minorversion": env.get("QB_MINORVERSION", "75").strip()}


class TokenStore:
    """Tokens in a small JSON file, written atomically so a crash cannot leave half a file."""

    def __init__(self, path=TOKEN_FILE):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)          # best effort; ignored on Windows
        except OSError:
            pass
        os.replace(tmp, self.path)


def make_auth_client(cfg):
    from intuitlib.client import AuthClient      # pip install intuit-oauth
    return AuthClient(cfg["client_id"], cfg["client_secret"], cfg["redirect_uri"], cfg["environment"])


def refresh_tokens(store, cfg, client_factory=make_auth_client):
    """Get a fresh access token and SAVE the rotated refresh token right away."""
    tokens = store.load()
    if not tokens or not tokens.get("refresh_token"):
        raise QBAuthError("not connected yet: run  python qb_auth.py connect")
    client = client_factory(cfg)
    try:
        client.refresh(refresh_token=tokens["refresh_token"])
    except Exception as e:
        raise QBAuthError(f"token refresh failed ({e}). Run  python qb_auth.py connect  again.") from e
    store.save({**tokens, "access_token": client.access_token,
                "refresh_token": client.refresh_token or tokens["refresh_token"], "refreshed_at": _now()})
    return store.load()


def parse_callback(url_or_path, expected_state):
    """Extract (code, realm_id) from the redirect URL. Rejects errors and a wrong `state`."""
    q = parse_qs(urlparse(url_or_path).query)
    one = lambda k: (q.get(k) or [None])[0]
    if one("error"):
        raise QBAuthError(f"Intuit returned an error: {one('error')}")
    if one("state") != expected_state:
        raise QBAuthError("state mismatch: this redirect does not belong to this login attempt")
    if not one("code"):
        raise QBAuthError("no authorization code in the redirect URL")
    return one("code"), one("realmId")


def wait_for_callback(redirect_uri, timeout=300):
    """Run a tiny local web server and return the path (with query) Intuit redirects to."""
    parsed = urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path == (parsed.path or "/"):
                got["path"] = self.path
                body = b"QuickBooks connected. You can close this tab and go back to the terminal."
            else:
                body = b"not found"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer((host, port), Handler)
    server.timeout = 5
    deadline = time.time() + timeout
    try:
        while "path" not in got and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "path" not in got:
        raise QBAuthError("timed out waiting for the redirect; try  python qb_auth.py connect --manual")
    return got["path"]


def connect(cfg, store, client_factory=make_auth_client, manual=False, open_browser=True,
            input_fn=input, scopes=None, wait_fn=wait_for_callback):
    if scopes is None:
        from intuitlib.enums import Scopes
        scopes = [Scopes.ACCOUNTING]
    state = secrets.token_urlsafe(16)
    client = client_factory(cfg)
    url = client.get_authorization_url(scopes, state_token=state)
    print("Open this URL, log in and click Allow:\n\n  " + url + "\n")
    if open_browser and not manual:
        webbrowser.open(url)
    if manual:
        redirected = input_fn("After Allow, paste the FULL URL from the address bar here: ").strip()
    else:
        redirected = wait_fn(cfg["redirect_uri"])
    code, realm_id = parse_callback(redirected, state)
    client.get_bearer_token(code, realm_id=realm_id)
    store.save({"access_token": client.access_token, "refresh_token": client.refresh_token,
                "realm_id": realm_id or client.realm_id, "environment": cfg["environment"],
                "connected_at": _now()})
    print(f"Connected (realm {realm_id or client.realm_id}, {cfg['environment']}). Tokens saved to {store.path}.")


def status(store):
    t = store.load()
    if not t:
        print("Not connected. Run: python qb_auth.py connect")
        return
    print(f"Connected: realm {t.get('realm_id')}, environment {t.get('environment')}, "
          f"connected {t.get('connected_at')}, last refresh {t.get('refreshed_at', 'never')}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "connect":
            connect(load_config(), TokenStore(), manual="--manual" in sys.argv)
        elif cmd == "status":
            status(TokenStore())
        else:
            print(__doc__)
    except QBAuthError as e:
        sys.exit(f"ERROR: {e}")
