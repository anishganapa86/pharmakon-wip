# Work split — Anish / Punya

Deadline: end of September. ~24 days from Sept 6.

## The rule that makes this work

`docs/api-contract.md` is frozen at the end of week 1. After that it changes
only by agreement of both people, in a PR that touches nothing else.

Punya builds against a **mock server** from day one and never blocks on the
engine being finished. Anish builds the engine against **tests**, not against
the UI. Neither person's progress depends on the other's daily output.

## Anish — engine and API

Owns `app/pk/`, `app/detect/`, `app/api/`, the substance registry, and FAERS validation.

- [x] Bateman single dose + superposition + Cockcroft–Gault (`app/pk/`)
- [x] Duplication and accumulation detectors (`app/detect/alerts.py`)
- [x] Test suite, 38 tests green
- [ ] FastAPI routes implementing the frozen contract
- [ ] Postgres/SQLAlchemy models, curve grid caching + invalidation on new dose
- [ ] Registry to 20+ substances, `verified=true` on every one
- [ ] FAERS extraction — **start week 2, not week 4**

## Punya — client and ingredient decomposition

Owns `client/`, the openFDA integration, and the normalization layer.

- [ ] React + TS + Recharts skeleton, deployed to Vercel in week 1
- [ ] Mock API server returning contract-shaped fixtures
- [ ] **Today** screen — logging under 30 seconds. Build this first, it's the
      screen everything else depends on
- [ ] **Curves** screen with threshold lines overlaid
- [ ] **Alerts** screen, each alert expandable to its contributing doses
- [ ] openFDA label API → ingredient decomposition
- [ ] Alias normalization layer (generics / brands / salt forms) + manual entry
      fallback

The normalization layer is the sleeper task. openFDA returns
`ACETAMINOPHEN`, `Acetaminophen`, `APAP`, and misspellings, all pointing at the
same substance. Budget a week for it and it still won't be enough. The manual
entry fallback is what keeps the demo working when normalization misses.

## Schedule

| Week | Anish | Punya | Gate |
|---|---|---|---|
| 1 (Sep 6–13) | Routes + DB on frozen contract | Deployed skeleton + Today screen on mocks | Contract frozen; both deployed |
| 2 (Sep 14–20) | Registry to 20, **FAERS extraction starts** | openFDA decomposition + normalization | Registry verified |
| 3 (Sep 21–27) | FAERS backtest numbers | Curves + Alerts on real API | **Feature freeze Sep 27** |
| 4 (Sep 28–30) | Bugs only | Bugs only | Clean-session demo works |

## What gets cut, in this order

Decided now so nobody has to argue about it at 2am on Sep 28.

1. Caregiver dashboard — already cut
2. Pattern/correlation engine — already cut, do not reopen
3. Plain-English symptom input — already cut
4. Automated FAERS pipeline → hand-curate ~50 cases instead
5. Registry 20 → 12 substances
6. Renal adjustment UI (keep the math, ship without the profile screen)

The **math is not on this list.** If the engine is unfinished, the project has
nothing. Cut features, not the model.

## Merge hygiene

Two people, one repo, three weeks. `main` is protected, everything through PRs,
CI runs `pytest` on every push. Anish never edits `client/`, Punya never edits
`app/pk/`. Shared files (`docs/api-contract.md`, `app/registry/substances.json`)
change only in single-purpose PRs.
