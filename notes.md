# notes.md

Pharmakon models systemic drug concentration over time, detects hidden
active-ingredient duplication, and flags accumulation toward clinically
meaningful thresholds.

**Not a medical device.** One-compartment model, population-average parameters,
educational and demonstrative use only. Keep that framing in any user-facing
copy you write.

## Commands

    source .venv/bin/activate       # .venv\Scripts\Activate.ps1 on Windows
    pytest                          # 803 tests (1 xfail), must be green
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
  trace back to specific doses is not actionable. The invariant (see
  `docs/api-contract.md`) is that an alert's counted doses
  (`contributing_doses`) and uncounted doses (`uncounted_doses`) are
  *together* non-empty. It is enforced at runtime in `app/api/service.py`:
  `recompute_alerts` raises `RuntimeError` for an alert with neither, and every
  alert response carries both lists. `uncounted_doses` is always `[]` today
  because the detectors don't produce uncounted doses yet (the
  `incomplete_data` follow-up, the one xfail). The `Alert` dataclass in
  `app/detect/alerts.py` has no `uncounted_doses` field and accepts an empty
  `contributing_dose_ids`, so the check lives in the service layer; don't rely
  on the dataclass to catch an untraceable alert.
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

## Decisions

- **MedlinePlus drug monograph text is not to be copied or quoted.** The
  monographs (the AHFS consumer drug information pages, including their
  "special dietary instructions" sections) are ASHP-copyrighted, not NLM
  public domain, and NLM's own terms allow linking to and displaying only
  what MedlinePlus Connect itself returns (title, link, attribution) --
  not reproducing page text. Food and drink guidance comes only from FDA
  label text via openFDA, quoted verbatim under the quote-integrity rules
  in `app/api/food_guidance.py`, or shown as a pointer when it can't be
  safely quoted.

## Configuration

- **`SHOW_UNVERIFIED_RULES`** (default off; see `.env.example`). Curated rules
  with `verified: false` are not applied while it is off: the rules in
  `app/data/drug_interactions.json` and `app/data/ingredient_interactions.json`,
  and the curated entries in `app/data/food_drug_guidance.json`. Each affected
  response then carries `rules_awaiting_verification` (`count`, `message`), and
  the Interactions, Skincare and Food tabs show "N rules are awaiting
  verification and are not being applied." Live FDA-label findings are
  unaffected. Truthy values are `1`, `true`, `yes` and `on`. `run.sh` and
  `run.ps1` default it to `1` for local development, `tests/conftest.py` turns
  it on for the suite, and `tests/test_verification_gate.py` turns it off
  explicitly. Registry thresholds and the detectors are not gated. The gate
  lives in `app/api/verification.py`.
- **`verified` means a person opened the source.** An entry can be
  `verified: true` only with `verified_by`, `verified_on`, `source_url` and
  `source_quote`; all three loaders reject it at load otherwise. Live
  FDA-label findings always return `verified: false`, plus
  `quote_source: "fda_label_verbatim"` where text is quoted.
- **`PHARMAKON_LABEL_CACHE_DIR`**: the on-disk openFDA label cache (default
  `.label_cache/`, gitignored, 7-day TTL; an empty value disables it, and the
  tests disable it). `python scripts/warm_label_cache.py` fills it for every
  registry substance and catalog combination product.

## Demo test

A browser and API check of the whole demo, separate from `pytest`. It starts
the API on a scratch SQLite file (never `pharmakon.db`) and a built client
preview, then writes `reports/demo_test.md` (gitignored).

    scripts/run_demo_test.sh                 # API smoke, desktop flow, phone layout
    scripts/run_demo_test.sh --repeat 2      # run twice and mark flaky steps
    scripts/run_demo_test.sh --gate-off      # SHOW_UNVERIFIED_RULES=0, gate tests only
    scripts/run_demo_test.sh --no-phone      # skip the 390px project
    cd client && npm run test:e2e            # Playwright desktop, servers already up
    cd client && npm run test:e2e:phone

Specs are in `client/e2e/`. A step that fails because of a real app bug is a
`test.fail` test through `knownBug` in `helpers.ts`, with the bug in its note.
The report lists those as "known bug"; a known-bug test that starts passing
fails the run, so remove its `knownBug` wrapper when the bug is fixed.

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
