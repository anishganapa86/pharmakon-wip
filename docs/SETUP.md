# Setup

Do this once. Should take about ten minutes.

## Before you start

Pharmakon lives in **its own folder, outside any other project.** If you have an
existing research or coursework folder open in VS Code, do not put this inside
it — a nested repo will confuse git, and the Jupyter extension will hand the
notebook whatever kernel that other project is using, which produces import
errors that look like code bugs but aren't.

## What to install

| Tool | Why | Where |
|---|---|---|
| Python 3.11 or newer | the engine | python.org, or `brew install python@3.11` |
| Git | the repo | git-scm.com (already on macOS) |
| VS Code | editor | code.visualstudio.com |
| Node 20+ | client only — Punya needs this, Anish doesn't yet | nodejs.org |

Check what you already have:

    python3 --version     # want 3.11+
    git --version
    node --version        # Punya only

VS Code extensions: open the repo, and VS Code will prompt you to install the
recommended set from `.vscode/extensions.json`. Say yes. That's Python,
Jupyter, Ruff, Prettier and ESLint — the same five for both of us, so formatting
never shows up as a diff.

## Clone and set up

**macOS / Linux**

    cd ~/code                     # or wherever you keep projects
    git clone https://github.com/punyagandhi/Pharmakon.git
    cd Pharmakon
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    pytest

**Windows (PowerShell)**

    cd ~\code
    git clone https://github.com/punyagandhi/Pharmakon.git
    cd Pharmakon
    py -3.11 -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    pytest

If PowerShell blocks the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

You should see **38 passed**. If you don't, stop and fix that before writing
anything — every later problem is easier to diagnose from a green baseline.

## Open it in VS Code

    code .

or File → **New Window** → Open Folder → `Pharmakon`. A separate window from
your other projects, not a second folder in an existing one.

Then `Cmd/Ctrl+Shift+P` → **Python: Select Interpreter** → pick the one whose
path contains `.venv`. VS Code remembers this per workspace, so it won't leak
between projects.

Check it took: the Testing sidebar (the flask icon) should list all 38 tests,
and running them from there should pass.

## The notebook

    notebooks/pharmakon_walkthrough.ipynb

Open it and click **Run All**. Pick the `.venv` kernel if prompted. It imports
from `app/`, so if a cell fails with `ModuleNotFoundError: app`, the interpreter
is wrong — reselect it.

The notebook is documentation, not a place to build things. Anything worth
keeping moves into `app/` with a test. Notebooks don't diff well in git and two
people editing one will conflict constantly.

## Daily loop

    source .venv/bin/activate       # .venv\Scripts\Activate.ps1 on Windows
    git pull
    pytest                          # green before you start
    # ...work...
    ruff check app tests --fix
    ruff format app tests
    pytest                          # green before you push
    git push

CI runs lint, format check, and tests on every push. A red build blocks the
merge — that's deliberate, so `main` is always demoable.

## Branches

`main` is protected. Work on a branch, open a pull request:

    git checkout -b engine/fastapi-routes     # Anish
    git checkout -b client/today-screen       # Punya

Anish does not edit `client/`. Punya does not edit `app/pk/`. Shared files —
`docs/api-contract.md` and `app/registry/substances.json` — change only in
pull requests that touch nothing else.

## If something breaks

**`ModuleNotFoundError: app`** — wrong interpreter. Reselect `.venv`.

**Tests pass in the terminal but not in VS Code** — the Testing sidebar is using
a different interpreter. Reselect, then reload the window.

**Formatting churn in a diff** — you're not on Ruff. Install the extension and
let `.vscode/settings.json` do its job.

**Notebook shows old outputs** — Restart, then Run All. Committed outputs are
from whoever ran it last.

## Accounts and sign-in

Every API route except `POST /auth/signup`, `POST /auth/login` and
`GET /health` needs a logged-in session. Sign-in is email and password; there
is no email verification and no password reset. The session lives in an
`HttpOnly`, `SameSite=Lax` cookie named `pharmakon_session`, valid for seven
days and renewed while it is used.

Settings come from environment variables, loaded from a gitignored `.env`
(copy `.env.example`).

| Variable | Meaning |
|---|---|
| `PHARMAKON_COOKIE_SECURE` | Set to `1` whenever the site is served over HTTPS so the cookie is marked `Secure`. Truthy values are `1`, `true`, `yes`, `on`. |
| `PHARMAKON_CORS_ORIGINS` | Comma-separated origins allowed to call the API with cookies (default: the two Vite dev origins). A `*` is ignored. A `POST`, `PUT`, `PATCH` or `DELETE` whose `Origin` header is not on this list is refused with `403`. |
| `PHARMAKON_DEV_DOCS` | Set to `1` to serve `/docs`, `/redoc` and `/openapi.json` to a logged-in user. Off by default. Read on every request. |

### Login throttling is per process

Failed logins and sign-up attempts are counted in memory, by IP and email
together and by IP alone, and a lockout doubles each time it is hit again,
with a `Retry-After` header on the `429`. The counters live inside one API
process. Restarting the API clears them, and running several workers gives
each worker its own counters, so the effective limit is multiplied by the
number of workers. Run a single worker for the demo. The IP is the address of
the direct connection, so behind a reverse proxy every visitor shares the
proxy's address until a trusted-proxy setting is added.
