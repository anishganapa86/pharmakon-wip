# Pharmakon

Models systemic drug concentration over time, detects hidden active-ingredient
duplication, and flags accumulation toward clinically meaningful thresholds.

**Not a medical device.** One-compartment model with population-average
parameters. Outputs are estimates, and can deviate substantially for anyone
with atypical metabolism. Educational and demonstrative only.

## Quick start

Full instructions, both OSes, in **docs/SETUP.md**. Short version:

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    pytest

## Layout

    app/pk/           Bateman, superposition, Cockcroft-Gault
    app/detect/       duplication + accumulation alerts
    app/api/          FastAPI (Anish)
    client/             React + TS + Recharts (Punya)
    app/registry/     registry + openFDA normalization
    tests/            38 tests, all green
    docs/SETUP.md     install and daily loop
    SPLIT.md          who builds what, and what gets cut first
    notebooks/        engine walkthrough (documentation, not a workspace)

## Invariants

- Concentrations are **mg/L** everywhere. Times are **hours**. Never mix units
  across a boundary.
- Timestamps are ISO 8601 with timezone, always.
- Every alert carries `contributing_dose_ids`. This is enforced by the
  dataclass, not by convention.
- Registry entries stay `verified: false` until a human has opened the cited
  source. Unverified is not the same as wrong, but it is not the same as right.
