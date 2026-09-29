# CLAUDE.md

Pharmakon models systemic drug concentration over time, detects hidden
active-ingredient duplication, and flags accumulation toward clinically
meaningful thresholds.

**Not a medical device.** One-compartment model, population-average parameters,
educational and demonstrative use only. Keep that framing in any user-facing
copy you write.

## Commands

    source .venv/bin/activate       # .venv\Scripts\Activate.ps1 on Windows
    pytest                          # 316 tests (1 xfail), must be green
    ruff check app tests --fix
    ruff format app tests

CI runs lint, format check, and tests. All three must pass.

## Layout

    app/pk/engine.py         Bateman, superposition, time grids
    app/pk/individualize.py  Cockcroft-Gault, ke adjustment, notes
    app/detect/alerts.py     duplication + accumulation detectors
    app/registry/            registry JSON + openFDA name normalization
    app/api/main.py          FastAPI routes: doses, curve, alerts, product search,
                             symptom check
    app/api/service.py       ISO <-> hours boundary; calls pk and detect
    app/api/db.py            SQLite via SQLAlchemy: doses, ingredients, alerts
    app/api/products.py      synthetic seed product catalog + search
    app/api/side_effects.py  symptom lookup against openFDA adverse reactions
    client/                  React + TypeScript + Recharts (Vite): product search
                             and dose logging, curves, symptom check, alerts
    tests/                   pytest
    notebooks/               walkthrough — documentation, not a workspace

## Invariants — do not violate these without saying so explicitly

- **Concentrations are mg/L, times are hours, everywhere.** Conversion to ISO
  timestamps happens at the API boundary and nowhere else.
- **Every alert is traceable to specific doses.** An alert the user can't
  trace back to specific doses is not actionable. The intended invariant (see
  `docs/api-contract.md`) is that an alert's counted doses
  (`contributing_dose_ids`) and uncounted doses (`uncounted_doses`) are
  *together* non-empty. **This is not enforced at runtime yet.** The `Alert`
  dataclass only requires `contributing_dose_ids` to be passed; an empty list is
  accepted, and `uncounted_doses` doesn't exist yet. The runtime check is a
  pending follow-up; until it lands, don't rely on the dataclass to catch an
  untraceable alert.
- **Registry entries stay `verified: false`** until a human has opened the cited
  source. Never flip that flag yourself. Unverified is not the same as wrong,
  but it is not the same as right either.
- **Never invent a correction factor.** Renal impairment scales `ke` via
  Cockcroft-Gault because that equation is published and validated. Hepatic
  impairment does not get a numerical adjustment, because no validated general
  one exists — it surfaces as a note and as a lowered threshold. If you're
  tempted to add a plausible-looking coefficient, add a note instead.
- **Refuse rather than guess.** Zero-order substances (alcohol) raise
  `UnsupportedKinetics`. Unmatched openFDA ingredient names return `None` and
  route to manual entry. A wrong curve or a mis-attributed dose is worse than
  an honest gap.
- **Silence is a feature in the detectors.** Staying quiet when the data doesn't
  support a finding is harder and more important than firing an alert. If you
  loosen a detection threshold, add a test that proves it still stays quiet on
  the negative case.

## Out of scope — decided, do not reopen

The Overview doc describes four systems. We build one. Cut and staying cut:
the pattern/correlation engine and the caregiver dashboard. Plain-English
symptom parsing is replaced by a simpler feature, now implemented: symptom
lookup against FDA adverse reaction labels (`app/api/side_effects.py`,
`POST /symptom-check`). It reports an association, never a cause. Also out
of scope: topical-only substances (retinoids,
AHAs, cleansers) — those are barrier-irritation problems, not pharmacokinetic
ones. And no diagnosis, no medication recommendation, no efficacy prediction.

## Working style

- Write the test first for anything in `app/pk/` or `app/detect/`. The suite is
  the reason to trust the engine.
- Prefer boring, well-documented tools. NumPy and SciPy, not exotic deps.
- Don't edit notebooks programmatically — they diff badly. Logic worth keeping
  moves into `app/` with a test.
- If a docstring or comment explains *why* a limit exists, keep it when
  refactoring. Those are the parts a reviewer reads.

## Ownership

Anish: `app/`, registry, FAERS backtest. Punya: `client/`, openFDA integration.
Shared files (`docs/api-contract.md`, `app/registry/substances.json`) change
only in single-purpose PRs. See SPLIT.md.
