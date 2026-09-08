# Contributing

Thanks for your interest. This is a small project — issues and PRs welcome.

## Development

The Python services (`agent/`, `manager/`) are plain FastAPI apps.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r manager/requirements.txt -r agent/requirements.txt

# smoke-test the manager against a throwaway config
export SP_MANAGER_CONFIG=/tmp/manager.toml STATE_DIRECTORY=/tmp/erf-state
python -c "import sys; sys.path.insert(0,'manager'); import app; print(len(app.app.routes))"
```

- Byte-compile before pushing: `python -m py_compile agent/*.py manager/*.py`
- Shell scripts: `bash -n` at minimum; `shellcheck` if you have it.
- Target Python **3.11+** on hosts (the code uses `tomllib`); `deploy.sh` keeps
  working on a 3.9 workstation.

## Conventions

- Many small files over few large ones. Functions under ~50 lines.
- Handle errors explicitly; never swallow them silently.
- No secrets in the repo. `secrets/` is gitignored; keep it that way.
- Commit messages: `type: summary` (`feat`, `fix`, `docs`, `refactor`, `chore`).

## What would help most

- Docker-in-Podman support in the runner image for workflows using `services:`
  or container jobs.
- Debian/Ubuntu host support in `setup-host.sh` (currently `dnf`-only).
- A pull-through registry mirror option to avoid re-pulling the base image per host.

## Reporting security issues

Please open a normal issue describing the *class* of problem — do not include a
working exploit or a step-by-step extraction path.
