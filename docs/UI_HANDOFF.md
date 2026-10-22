# UI handoff from the demo test

For Punya. Everything here is client work (`client/`); nothing needs a server
change unless noted. Source: `reports/demo_test.md` from the last
`scripts/run_demo_test.sh --repeat 2` on branch `demo-test` (two runs, no flaky
steps). Screenshot paths are relative to the repository root and live in
`reports/` (gitignored), so re-run the script to regenerate them.

Not a medical device. Educational and demonstrative use only.

## Remaining UI bugs

| # | Screen | Expected | Saw | Screenshot |
|---|---|---|---|---|
| 1 | Log a dose, quantity 0 or -1 | A visible message: "Enter a quantity greater than 0" | The Log button is disabled and nothing explains why | `reports/runs/r1/screenshots/bug-flow-spec-ts-k-quantity-0-and-1-show-a-clear-message.png` |
| 2 | Schedule card after "Take now" outside the slot window | A status that says the dose was taken | Status stays "Upcoming" (see the schedule rule below) | none yet; reproduce by scheduling at 08:00 after 08:00 and tapping "Take now" |
| 3 | Food & drink | Every quoted label sentence ends with terminal punctuation | "When using this product take with food or milk if stomach upset occurs" has none (the FDA label text has none) | `reports/runs/r1/screenshots/bug-flow-spec-ts-f-every-food-drink-quote-ends-with-terminal-punctuation.png` |
| 4 | Skincare | A finding that names every logged product containing niacinamide | Niacinamide appears only inside the pairwise vitamin C findings, so the La Roche-Posay cleanser (which contains niacinamide) is not named by any finding | `reports/runs/r1/screenshots/bug-flow-spec-ts-g-a-niacinamide-card-lists-every-product-containing-niacinamide.png` |
| 5 | Phone 390x844, all tabs | No horizontal page overflow | The page is 142px wider than the viewport | `reports/runs/r1/screenshots/phone-concentration.png`, `phone-timeline.png`, `phone-skincare.png`, `phone-interactions.png`, `phone-food-drink.png` |
| 6 | Phone, Interactions and Food & drink tabs | The tab can be tapped | The tab bar overflows and the tab buttons overlap: "Skincare intercepts pointer events" for Interactions, "Interactions intercepts pointer events" for Food & drink | `reports/runs/r1/screenshots/bug-phone-spec-ts-phone-interactions-tab-fits-a-390px-screen.png`, `bug-phone-spec-ts-phone-food-drink-tab-fits-a-390px-screen.png` |
| 7 | Phone, all tabs | Buttons, tabs and summary rows at least 40px tall | 23px: "Hide", "Remove", "Delete", "Load more", "+ Add another product", and every `summary` ("Ingredients (n)", "Products without X", "Side effects of your current regimen"). 33px: the "Recent" product buttons | `reports/runs/r1/screenshots/bug-phone-spec-ts-phone-concentration-tab-fits-a-390px-screen.png`, `bug-phone-spec-ts-phone-timeline-tab-fits-a-390px-screen.png`, `bug-phone-spec-ts-phone-skincare-tab-fits-a-390px-screen.png` |

Items 3 and 4 are decisions as much as bugs. Item 3 is a quote-integrity
question (`app/api/food_guidance.py`): the sentence is verbatim, so the fix may
be how the client frames it (for example a trailing ellipsis outside the
quote marks), not editing the text. Item 4 may belong in the server's finding
shape; ask before changing the client.

Inputs on the phone were checked too: no input is under 16px, and no text is
clipped by `overflow: hidden`.

## Quantity message (bug 1)

Exact wording: `Enter a quantity greater than 0`

Show it inline, under or beside the quantity field in the "Log a dose" panel,
whenever the field is non-empty and `Number(quantity)` is not a finite number
above 0. The e2e test (`k. quantity 0 and -1 show a clear message`) looks for
an element inside the Log a dose panel with class `error`, `warning`,
`muted` or `role="alert"` whose text matches `quantity`, `greater than`,
`more than 0`, `at least` or `must be`. Keep the button disabled. The server
already answers `422` for these values, so this is only about explaining the
disabled button.

## Schedule status rule (bug 2)

The server decides status in `app/api/schedule.py` (`evaluate`, `summarize`):

- Each slot owns a window that opens 2 hours before the slot
  (`EARLY_WINDOW`) and closes at the next slot minus 2 hours, capped 24 hours
  after the slot (`MAX_LATE`).
- A logged dose of the same product counts for a slot only when it falls inside
  that window, and one dose satisfies at most one slot.
- A slot earlier than the schedule's `created_at` does not exist. A schedule
  made at 20:00 for 08:00 has no slot today; its next slot is 08:00 tomorrow,
  whose window opens at 06:00 tomorrow.
- A dose outside every window is an ordinary dose. It is logged, charted and
  alerted on, but it does not satisfy a slot, so `status` stays `upcoming`.

What `GET /users/{id}/schedule` returns for it (`POST .../take` returns `dose_event_id`
and `triggered_alerts`): per medication,

- `status`: `taken_today` | `due` | `overdue` | `upcoming` | `as_needed`
- `next_due`: ISO timestamp of the next slot to act on, or `null`
- `last_taken`: ISO timestamp of the most recent logged dose of this product
  by any route (manual log or "Take now"), or `null`
- `today[]`: today's slots, each with `scheduled_at`, `status`
  (`upcoming` | `due` | `overdue` | `taken` | `missed`), `taken_at` and
  `dose_event_id`

There is no server field for "taken outside the window". The client can derive
it today: when `status` is `upcoming` and `last_taken` is not
`null` and is later than the start of the local day (or later than the
schedule's `created_at`), show `Taken (outside the usual window)` and the
`last_taken` time in the card. Do not show it when `status` is `taken_today`,
and do not change `status` itself: only a dose inside a slot window may mark
the slot taken.

If you would rather have the server say it, add a boolean such as
`taken_outside_window` to the `schedule_json` response in
`app/api/schedule.py` and `docs/api-contract.md` (a shared file, so a
single-purpose PR). Ask Anish; that is a contract change.

## Checked and fine

Not bugs, so you can skip them: every other step of the demo flow passes on
desktop at 1280px, the awaiting-verification notice and the gated findings
behave correctly with `SHOW_UNVERIFIED_RULES=0`, and the Add-a-product flow,
timeline delete with confirm, the timeline scroll box, the "Unverified
reference" tags and the "Stop taking this" wording were all fixed on `main`.

Server fixes made alongside this doc, on `demo-test-fixes`: the 422 from
`POST /skincare-alternatives` (the client payload was correct; the server
rejected the ingredient "Alcohol" as a medication) and curated search by the
full displayed name.
