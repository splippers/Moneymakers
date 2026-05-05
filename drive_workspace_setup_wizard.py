#!/usr/bin/env python3
"""
Step-by-step Google Cloud + Workspace setup for Projectscan → Google Drive.

Most steps use console.cloud.google.com URLs you can open on any device (including
a mobile browser while signed into your Google Workspace account).

The Google Cloud Console steps need only a browser (phone or desktop).

OAuth user consent (“drive-auth”) uses a localhost redirect on the machine where
Python runs. Over SSH use a fixed port plus ``ssh -L`` so your **local**
browser completes that redirect (tunnelled to the server).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

# Fixed loopback port for SSH local forwarding (must match -L and drive-auth).
OAUTH_SSH_PORT = 9876


def _script_parent() -> Path:
    return Path(__file__).resolve().parent


def _index_dir() -> Path:
    raw = os.environ.get("PROJECTSCAN_INDEX_DIR")
    if raw:
        return Path(raw).expanduser()
    return _script_parent() / "project_index"


def _pause(title: str) -> None:
    try:
        input(f"\n{title}")
    except EOFError:
        print("(non-interactive: continuing)\n", file=sys.stderr)


def _print_url(label: str, url: str) -> None:
    bar = "─" * min(72, max(48, len(url) + 4))
    print(f"\n{label}\n{bar}\n{url}\n")


def _accounts_signin_continue(dest_url: str) -> str:
    """accounts.google.com link that redirects to dest after signing in."""
    qs = urllib.parse.urlencode({"continue": dest_url})
    return f"https://accounts.google.com/ServiceLogin?{qs}"


def _wizard(project_id_arg: str | None, emails_arg: list[str], skip_drive_auth_prompt: bool) -> int:
    print(
        """
╔══════════════════════════════════════════════════════════════════════════╗
║  Projectscan — Google Workspace + Drive OAuth setup                     ║
╚══════════════════════════════════════════════════════════════════════════╝

You’ll use INTERNAL OAuth (Workspace-only) unless you deliberately choose External.
URLs below are formatted for tap → copy → open in Safari/Chrome on a phone."""
    )

    proj = (
        project_id_arg
        or input("\n► Google Cloud project ID (create one first if needed): ").strip()
    )
    if not proj:
        print("Project ID is required.", file=sys.stderr)
        return 1

    _print_url(
        "Step 1 — Pick or create project (desktop is easier to name it; mobile works too)",
        f"https://console.cloud.google.com/cloud-resource-manager?project={proj}",
    )
    print(
        "If this project doesn’t exist yet:\n"
        "  https://console.cloud.google.com/projectcreate\n"
        "Pick the same PROJECT ID printed in the GCP header when finished."
    )
    _pause("When the project exists and header shows this project ID → press Enter.")

    enable_drive = f"https://console.cloud.google.com/apis/library/drive.googleapis.com?project={proj}"
    _print_url("Step 2 — Enable Google Drive API (tap ENABLE)", enable_drive)
    _print_url("   └─ If prompted to log in:", _accounts_signin_continue(enable_drive))
    _pause("After Drive API shows Enabled → press Enter.")

    consent = f"https://console.cloud.google.com/apis/credentials/consent?project={proj}"
    _print_url("Step 3 — OAuth consent screen", consent)
    _print_url("   └─ Workspace sign-in helpers:", _accounts_signin_continue(consent))

    scopes_block = """Add these scopes (Edit → Scopes → add manually if needed):

  • https://www.googleapis.com/auth/drive.file
  • https://www.googleapis.com/auth/userinfo.email

User type: INTERNAL (restricts login to organisational accounts).

App name any label you like (“Projectscan”). Save."""
    print(scopes_block)
    _pause("When consent screen is saved as INTERNAL + scopes added → press Enter.")

    credentials_url = f"https://console.cloud.google.com/apis/credentials?project={proj}"
    _print_url("Step 4 — Create OAuth Desktop client credentials", credentials_url)
    _print_url("   └─ Sign-in shortcut:", _accounts_signin_continue(credentials_url))

    client_path = (_index_dir() / "client_secrets.json").resolve()
    print(
        f"""
Create credential → OAuth client ID → Application type DESKTOP APP.
Download JSON → save as:\n\n  {client_path}\n
(Parent folders are created when you pip / run projectscan.)
"""
    )
    _pause("When client_secrets.json is on disk → press Enter.")

    allow = ",".join(e.strip().lower() for e in emails_arg if e.strip())
    if not allow:
        raw = (
            input(
                "\n► Comma-separated allowed upload emails "
                "(e.g. jon@splippers.com) or Enter to skip app-side filter: "
            )
            .strip()
            .lower()
        )
        allow = ",".join(p.strip().lower() for p in raw.split(",") if p.strip())

    print("\n--- Step 5 — Environment (copy into profile or systemd) ---\n")
    if allow:
        print(f'export PROJECTSCAN_DRIVE_ALLOWED_EMAILS="{allow}"')
    else:
        print("# PROJECTSCAN_DRIVE_ALLOWED_EMAILS unset → any Drive account could upload.")

    tok = (_index_dir() / "google_drive_token.json").resolve()
    folder_help = "# Optional: folder for uploads\n# export PROJECTSCAN_DRIVE_FOLDER_ID=…"
    print(f"""export PROJECTSCAN_DRIVE_CLIENT_SECRETS="{client_path}"

# Use a fixed OAuth callback port when using SSH (-L forwards this to the server).
export PROJECTSCAN_DRIVE_OAUTH_PORT={OAUTH_SSH_PORT}

{folder_help}

# Existing token ({tok}) MUST be regenerated if scopes changed:
# rm -f "{tok}"

--- end snippet ---""")
    optional_admin = "https://admin.google.com/ac/owl/list?tab=configuredAPIs"

    drive_auth_intro = f"""Step 6 — OAuth consent over SSH (full flow can be remote)

1) On your **laptop** (not inside ssh), open a tunnel — leave it running:

     ssh -L {OAUTH_SSH_PORT}:127.0.0.1:{OAUTH_SSH_PORT} -N USER@REMOTE_HOST

2) In your existing SSH session to REMOTE_HOST, run drive-auth below. When a URL
   appears, open it in Chrome/Safari **on the laptop** (still works if projectscan runs on the server).

3) Google redirects to http://127.0.0.1:{OAUTH_SSH_PORT}/ — hits your laptop loopback → tunnel → remote server."""

    print("\nIf Drive sign-in fails for users, Workspace admin might block API access:")
    print(optional_admin)

    print(f"\n{drive_auth_intro}\n")
    _print_url(
        "(Reference) Why localhost / loopback is required for desktop OAuth",
        "https://developers.google.com/identity/protocols/oauth2/native-app#redirect-uri_loopback",
    )

    venv_py = _script_parent() / ".venv" / "bin" / "python"
    scan_py = _script_parent() / "projectscan.py"
    exe = (
        str(venv_py)
        if venv_py.is_file()
        else "python3"
    )
    drive_auth_cmd = (
        f'{exe} "{scan_py.resolve()}" drive-auth --oauth-port {OAUTH_SSH_PORT}'
    )

    if allow:
        prefix = f'PROJECTSCAN_DRIVE_ALLOWED_EMAILS="{allow}" \\\n  '
    else:
        prefix = ""

    print(
        f"""
Run once on the SSH host after the tunnel is up:

{prefix}{drive_auth_cmd}

Then test upload:

{prefix}{exe} "{scan_py.resolve()}" drive-upload --format txt --subset all

If USER@REMOTE_HOST is this box, use the same SSH target you normally use."""
    )

    if not skip_drive_auth_prompt:
        if input("\nRun drive-auth now from this script? [y/N] ").strip().lower() in ("y", "yes"):
            env = os.environ.copy()
            env["PROJECTSCAN_DRIVE_OAUTH_PORT"] = str(OAUTH_SSH_PORT)
            if allow:
                env["PROJECTSCAN_DRIVE_ALLOWED_EMAILS"] = allow
            rc = subprocess.call(drive_auth_cmd, shell=True, cwd=str(_script_parent()), env=env)
            return rc

    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Workspace-oriented Drive OAuth setup URLs + drive-auth guidance."
    )
    ap.add_argument("--project-id", help="Google Cloud project ID (skips question)")
    ap.add_argument(
        "--allow-email",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Allowed upload address (repeat or use comma in interactive prompt). "
        "Sets PROJECTSCAN_DRIVE_ALLOWED_EMAILS when printed.",
    )
    ap.add_argument(
        "--skip-run-drive-auth",
        action="store_true",
        help="Do not offer to launch drive-auth at the end.",
    )
    args = ap.parse_args()
    raise SystemExit(
        _wizard(
            project_id_arg=args.project_id,
            emails_arg=args.allow_email,
            skip_drive_auth_prompt=args.skip_run_drive_auth,
        )
    )


if __name__ == "__main__":
    main()
