#!/usr/bin/env python3
"""
Google OAuth with an HTTP callback reachable from your phone on the LAN.

Prerequisites
-------------
1. OAuth client JSON from Google Cloud Console.

   Desktop clients usually only allow http://127.0.0.1 redirects. For mobile-on-LAN,
   create an OAuth client of type **Web application** and add this exact redirect URI:

      http://REDIRECT_HOST:PORT/

   Example: http://192.168.1.2:8766/

2. Python deps (use a venv if your distro blocks pip install --user):

      pip install google-api-python-client google-auth-oauthlib google-auth-httplib2

Run (from repo root or anywhere):

      python3 scripts/gdrive_oauth_lan_quota.py \\
        --client-secrets /path/to/client_secret_....json \\
        --redirect-host 192.168.1.2 \\
        --port 8766

Open the printed URL on your phone (same Wi‑Fi). After success, Drive quota prints and
optional token JSON is written (--token-out).

Scopes: drive.metadata.readonly (enough for Drive about.storageQuota).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]


def _guess_lan_ip() -> str:
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def human_gb(n: float) -> str:
    if n <= 0:
        return "0 GiB"
    return f"{n / (1024 ** 3):.2f} GiB"


def make_handler_class(fallback_host: str):
    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        server_version = "GDriveOAuthLAN/1.0"

        def log_message(self, fmt: str, *args) -> None:
            print("[callback]", fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            srv = self.server
            host_hdr = self.headers.get("Host") or fallback_host
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path in ("/favicon.ico", "/robots.txt"):
                self.send_response(204)
                self.end_headers()
                return

            qs = urllib.parse.parse_qs(parsed.query)
            err = (qs.get("error") or [None])[0]
            if err:
                srv.oauth_error = err
                body = f"<html><body><pre>OAuth error: {err}</pre></body></html>"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
                threading.Thread(target=srv.shutdown, daemon=True).start()
                return

            code = (qs.get("code") or [None])[0]
            if code:
                srv.authorization_response = f"http://{host_hdr}{self.path}"
                html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width"/></head>
<body style="font-family:system-ui,sans-serif;padding:1.5rem;line-height:1.45;">
  <p><strong>Done.</strong> You can close this tab.</p>
</body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
                threading.Thread(target=srv.shutdown, daemon=True).start()
                return

            self.send_response(404)
            self.end_headers()

    return OAuthCallbackHandler


def main() -> None:
    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "Missing libraries. Install with:\n"
            "  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2\n"
            "Example venv (fast path): python3 -m venv /tmp/mmgdrivevenv && "
            "/tmp/mmgdrivevenv/bin/pip install google-api-python-client google-auth-oauthlib google-auth-httplib2\n"
            "Then run: /tmp/mmgdrivevenv/bin/python scripts/gdrive_oauth_lan_quota.py ...",
            file=sys.stderr,
        )
        sys.exit(1)

    ap = argparse.ArgumentParser(description="Google Drive OAuth (LAN callback) + quota.")
    ap.add_argument(
        "--client-secrets",
        type=Path,
        required=True,
        help="OAuth client JSON from Google Cloud (often named client_secret_*.json).",
    )
    ap.add_argument(
        "--redirect-host",
        default=_guess_lan_ip(),
        help="Host/IP embedded in redirect_uri (must match GCP authorized redirect URI). Default: guess LAN IP.",
    )
    ap.add_argument("--listen-host", default="0.0.0.0", help="Bind address for callback server.")
    ap.add_argument("--port", type=int, default=8766, help="Callback TCP port.")
    ap.add_argument(
        "--token-out",
        type=Path,
        default=None,
        help="Write OAuth token JSON here (optional).",
    )
    ap.add_argument("--timeout", type=int, default=600, help="Seconds to wait for callback.")
    args = ap.parse_args()

    secrets = args.client_secrets.expanduser().resolve()
    if not secrets.is_file():
        sys.exit(f"client secrets not found: {secrets}")

    redirect_uri = f"http://{args.redirect_host}:{args.port}/"
    fallback_host = f"{args.redirect_host}:{args.port}"

    flow = Flow.from_client_secrets_file(str(secrets), scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, _state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")

    Handler = make_handler_class(fallback_host)
    httpd = HTTPServer((args.listen_host, args.port), Handler)
    httpd.authorization_response = None  # type: ignore[attr-defined]
    httpd.oauth_error = None  # type: ignore[attr-defined]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    print("")
    print("--- Google OAuth (open on your phone, same LAN) ---")
    print(f"Redirect URI (must be authorized in GCP): {redirect_uri}")
    print("")
    print(auth_url)
    print("")
    print(f"Listening on http://{args.listen_host}:{args.port}/ (reachable at {redirect_uri})")
    print(f"Waiting up to {args.timeout}s…")
    print("")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if httpd.authorization_response or httpd.oauth_error:
            break
        time.sleep(0.2)

    try:
        httpd.shutdown()
    except Exception:
        pass
    thread.join(timeout=5)

    if httpd.oauth_error:
        sys.exit(f"OAuth failed: {httpd.oauth_error}")
    if not httpd.authorization_response:
        sys.exit("Timed out waiting for OAuth callback (no code received).")

    flow.fetch_token(authorization_response=httpd.authorization_response)
    creds = flow.credentials

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    about = drive.about().get(fields="user,storageQuota").execute()
    user = about.get("user") or {}
    sq = about.get("storageQuota") or {}

    limit = int(sq.get("limit") or 0)
    usage = int(sq.get("usage") or 0)
    usage_drive = int(sq.get("usageInDrive") or 0)
    usage_trash = int(sq.get("usageInDriveTrash") or 0)

    email = user.get("emailAddress") or "(unknown)"
    print("")
    print("=== Google Drive storage ===")
    print(f"Account (Drive token): {email}")
    print(f"Total used (all Google storage counted here): {human_gb(usage)} ({usage} bytes)")
    print(f"Drive used:            {human_gb(usage_drive)} ({usage_drive} bytes)")
    print(f"Drive trash:           {human_gb(usage_trash)} ({usage_trash} bytes)")
    print(f"Quota limit:           {human_gb(limit)} ({limit} bytes)")
    if limit > 0:
        pct = 100.0 * usage / limit
        print(f"Overall usage:         {pct:.1f}% of quota limit")
    print("")

    if args.token_out:
        out = args.token_out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
        out.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        print(f"Wrote token JSON: {out}")
        print("(Keep this file private.)")


if __name__ == "__main__":
    main()
