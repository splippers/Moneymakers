# Moneymakers

Scan a directory of git repositories, apply monetization heuristics, and prioritise what to work on next. Optional local dashboard and Google Drive export.

**Repository:** https://github.com/splippers/Moneymakers

## Requirements

- Python 3.10+
- Core tooling uses only the standard library.
- Google Drive uploads require optional dependencies (see below).

## Quick start

From this repository root:

```bash
python3 projectscan.py              # scan sibling folders under the parent of this repo
python3 projectscan.py serve        # dashboard at http://127.0.0.1:8765
```

Point at another tree of projects:

```bash
export PROJECTSCAN_ROOT=/path/to/projects
python3 projectscan.py serve
```

### Optional: Google Drive

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-google.txt   # or: pip install ".[google]"
```

Place Google OAuth **Desktop** client JSON as `project_index/client_secrets.json`, then:

```bash
python3 projectscan.py drive-auth
python3 projectscan.py drive-upload --format txt --subset all
```

Interactive GCP / Workspace URL walkthrough (mobile-friendly):

```bash
./drive_workspace_setup_wizard.py
```

## Layout

| Path | Role |
|------|------|
| `projectscan.py` | Scan, score, HTTP dashboard, report APIs, Drive integration |
| `drive_workspace_setup_wizard.py` | Step-by-step OAuth / GCP console helpers |
| `requirements-google.txt` | Pin-compatible optional Google client libraries |
| `Meta-Cursor-heuristic-algorythm-guidance.md` | Scoring rubric notes for agents / Cursor |
| `project_index/` | Local-only: scan results (`repos.json`, `repos.csv`) and OAuth secrets — not tracked in git |

Environment variables are documented in the docstrings at the top of `projectscan.py`. Notable extras: **`PROJECTSCAN_EXTRA_ROOTS`** — comma-separated paths of additional git repos to merge into one scan — and the repo that contains **`projectscan.py`** is always included when it has a **`.git`** directory (so this tool scores itself even if **`PROJECTSCAN_ROOT`** points at another tree).

## Licence

Add a `LICENSE` file when you publish; none is bundled yet.
