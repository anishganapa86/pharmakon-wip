# Pharmakon API contract

Status: **DRAFT — not frozen.** Freezes when Anish and Punya both approve it in
a single-purpose PR. After that it changes only by agreement of both, in a PR
that touches nothing else (see SPLIT.md).

Endpoints and top-level field names come from the technical spec §7 (the
technical spec PDF in `docs/` is the only source of truth; check this document
against it once it's added). Anything the spec did not state (types, nullability, nested shapes) is marked
**[filled in]** so it can be challenged before the freeze. The seven open
questions from the first draft have been decided; see
[Decision log](#decision-log).

## Global rules

These apply to every endpoint. A response that violates one is a bug.

### Units

| Quantity | Unit | Notes |
|---|---|---|
| Concentration | **mg/L** | The only concentration unit in this API. Never µg/mL, never mixed. Every response that carries a concentration also carries `"unit": "mg/L"`. |
| Mass | mg | Field names end in `_mg`. |
| Body weight | kg | `weight_kg` |
| Serum creatinine | mg/dL | `serum_creatinine_mg_dl` |
| Age | years | `age_years` |

### Timestamps

- ISO 8601 **with a timezone offset**, always: `2026-09-13T08:30:00-07:00` or
  `2026-09-13T15:30:00Z`.
- A timestamp without an offset (`2026-09-13T08:30:00`) is rejected with `422`.
  The server never guesses a timezone.
- The engine works in float hours internally. Conversion happens at the API
  boundary and nowhere else; hours never appear in the API.

### Traceability

Every alert carries two arrays of doses: `contributing_doses` (counted in the
alert's figure) and `uncounted_doses` (excluded from it). **For every alert,
the two are together non-empty.** Either one alone may be empty. An alert the
user can't trace back to specific doses is not actionable. The server must
never emit an alert with both arrays empty; the client may treat such an alert,
or one missing either array, as malformed.

This rule applies to every alert kind with no exceptions, so there is no
special case to forget.

### Causes

Every alert carries `notes`: a **required** array of strings, which may be
empty. This is where the cause of a caveat is shown, e.g. "No serum creatinine
on file, so population clearance was used" or a threshold lowered for hepatic
impairment. Never show a warning without showing its cause (spec §8).

### Authentication and data — v1 constraint

There is **no authentication in v1.** This is acceptable only under a hard
constraint:

> **The deployed demo runs on seeded synthetic data only. Neither Anish nor
> Punya — nor anyone else — logs real medications into the public instance.**

This is a constraint on how the system is operated, not a TODO. Any
deployment that accepts real personal medication data requires
authentication first, and that is a contract change.

### Notation

- **required** — always present; never `null`.
- **optional** — may be omitted from requests.
- **nullable** — always present in responses, but the value may be `null`.
- `{id}` is the user id, an opaque string. **[filled in]**

---

## 1. Log a dose

`POST /users/{id}/doses`

### Request

| Field | Type | Required | Description |
|---|---|---|---|
| `product_id` | string | required | From `GET /products/search`. |
| `taken_at` | string (ISO 8601 + tz) | required | When the dose was taken, not when it was logged. |
| `quantity` | number | required | Count of product units (e.g. 2 tablets). Must be > 0. The server converts to mg per ingredient using the product's ingredient strengths. **[filled in]** |

Products with unmodeled ingredients **are loggable** (manual-entry fallback,
spec §5). An ingredient is unmodeled for one of two reasons, carried per
ingredient as `unmodeled_reason` (see [Ingredient](#4-product-search)):

- **`unmatched`** — the name didn't normalize to a registry substance.
  `substance_id` is `null`.
- **`unknown_strength`** — the substance is recognized (`substance_id`
  resolves normally) but the label gives no usable mass, so the ingredient's
  `amount_mg` for the dose is `null`.

Either way the ingredient is stored with the dose but **excluded from both the
PK model and the 24-hour duplication sum.** It is never silently dropped: the
product carries `has_unmodeled_ingredients: true`, and the UI must name which
ingredient is uncounted and why. The two reasons are different sentences to a
user ("we don't recognize this ingredient" vs "we recognize it but don't know
the dose") and must not be collapsed.

Why exclusion must be visible: a duplication total that silently omits an
unknown-strength contribution is worse than no total, because it reads as
authoritative. Acetaminophen is exactly where this bites.

```json
{
  "product_id": "prod_tylenol_es_500",
  "taken_at": "2026-09-13T08:30:00-07:00",
  "quantity": 2
}
```

### Response — `201 Created`

| Field | Type | Required | Description |
|---|---|---|---|
| `dose_event_id` | string | required | Id of the logged dose. The same id appears in `contributing_doses` on alerts. |
| `triggered_alerts` | Alert[] | required | Alerts that became active **because of this dose**. Empty array if none — never omitted, never `null`. Same shape as `GET /alerts` (see [Alert](#alert-object)). |

```json
{
  "dose_event_id": "dose_01J8Z3",
  "triggered_alerts": []
}
```

---

## 2. Concentration curve

`GET /users/{id}/curve`

### Query parameters

| Param | Type | Required | Description |
|---|---|---|---|
| `substance_id` | string | required | Registry id, e.g. `acetaminophen`. |
| `from` | string (ISO 8601 + tz) | required **[filled in]** | Start of window, inclusive. URL-encode the `+` in offsets. |
| `to` | string (ISO 8601 + tz) | required **[filled in]** | End of window, inclusive. Must be after `from`. |
| `resolution` | string enum | optional | `15m` \| `1h`. Default `15m`. Any other value → `422`. The engine computes on a fixed 15-minute grid; `1h` is decimation of that grid. Arbitrary resolutions would require interpolation, which spec §8 forbids. |

### Response — `200 OK`

| Field | Type | Required | Description |
|---|---|---|---|
| `substance` | `{ id: string, display_name: string }` | required | **[filled in shape]** |
| `modelable` | boolean | required | `false` when the model cannot represent this substance (e.g. zero-order alcohol). **[filled in: present on every response]** |
| `reason` | string | nullable | Why the substance isn't modelable. `null` when `modelable` is `true`. |
| `unit` | string | required | Always `"mg/L"`. |
| `points` | `{ t: string, concentration: number }[]` | required | `t` is ISO 8601 + tz; `concentration` in mg/L, ≥ 0. Ordered by `t` ascending. Empty when `modelable` is `false`. |
| `threshold_concentration_mg_per_l` | number | nullable | A published toxic or therapeutic plasma concentration, in mg/L, from the registry field of the same name. **`null` for every substance at launch.** |
| `peak` | `{ t: string, concentration: number }` | nullable | Highest point within `[from, to]`. `null` if there are no doses in range or `modelable` is `false`. **[filled in shape]** |
| `current` | `{ t: string, concentration: number }` | nullable | Estimate at request time. `null` if "now" is outside `[from, to]` or `modelable` is `false`. **[filled in shape]** |

**Thresholds on curves.** `threshold_concentration_mg_per_l` is never derived
from a dose threshold such as `threshold_24h_mg`. That conversion depends on
volume of distribution, dose timing and accumulation state, so any computed
value would be invented. The field is populated only where a defensible
published concentration exists. The 24-hour dose total versus
`threshold_24h_mg` belongs on Alerts, not overlaid on a curve. **The Curves
screen must render correctly with no threshold line.**

```json
{
  "substance": { "id": "caffeine", "display_name": "Caffeine" },
  "modelable": true,
  "reason": null,
  "unit": "mg/L",
  "points": [
    { "t": "2026-09-13T16:00:00-07:00", "concentration": 0.0 },
    { "t": "2026-09-13T16:15:00-07:00", "concentration": 1.42 }
  ],
  "threshold_concentration_mg_per_l": null,
  "peak": { "t": "2026-09-13T16:45:00-07:00", "concentration": 2.87 },
  "current": { "t": "2026-09-13T16:15:00-07:00", "concentration": 1.42 }
}
```

### Unmodelable substances — still `200 OK`

A substance the model can't represent is a documented limit of the model, not
a client error. It mirrors the `unmodelable` alert kind in
`app/detect/alerts.py`.

```json
{
  "substance": { "id": "ethanol", "display_name": "Alcohol (ethanol)" },
  "modelable": false,
  "reason": "Zero-order elimination; the one-compartment first-order model cannot represent it.",
  "unit": "mg/L",
  "points": [],
  "threshold_concentration_mg_per_l": null,
  "peak": null,
  "current": null
}
```

---

## 3. Alerts

`GET /users/{id}/alerts`

### Query parameters

| Param | Type | Required | Description |
|---|---|---|---|
| `active` | string enum | optional | `true` \| `false` \| `all`. Default `true`. `true` returns only active alerts; `false` returns **only inactive** alerts; `all` returns both. Any other value → `422`. |

### Response — `200 OK`

An array of [Alert](#alert-object). Empty array if none.

### Active and inactive alerts

An alert is active if and only if both `dismissed_at` and `resolved_at` are
`null`. The schema allows two ways out:

1. **Resolved** — recomputation no longer produces it: the 24-hour window has
   rolled past the contributing doses, or a profile change moved the threshold.
   `resolved_at` is set.
2. **Dismissed** — the user dismisses it. `dismissed_at` is set.

**Dismissal is not in v1.** No v1 endpoint writes `dismissed_at`; it is
always `null` in v1, and **only recomputation sets an alert inactive.** The
field stays in the schema so adding dismissal later is not a breaking change.
When added, the intended shape is:

    POST /users/{id}/alerts/{alert_id}/dismiss

Until then, client code must not assume any alert can be dismissed, and server
code must not set `dismissed_at` by any other path.

**There is no auto-expiry.** An alert never becomes inactive purely because
time passed. If the user is still over the limit, the alert is active at hour
23 and at hour 25 alike. The recompute decides, not a timer.

### Alert kinds

| Kind | Severity | Fires when |
|---|---|---|
| `duplication` | `caution` \| `urgent` | Counted 24h total of one substance, across more than one product, reaches the warning fraction of `threshold_24h_mg`. |
| `threshold` | `caution` \| `urgent` | Same, from a single product. |
| `accumulation` | `caution` | Daily trough concentrations are rising. |
| `unmodelable` | `info` | The substance can't be modeled (e.g. zero-order alcohol). |
| `incomplete_data` — substance-level | `caution` | A **known substance** with a `threshold_24h_mg` has one or more `unknown_strength` doses in the 24h window — **regardless of the counted total.** `substance` is set. |
| `incomplete_data` — product-level | `caution` **[filled in]** | Any logged product in the 24h window has an **`unmatched` ingredient** — regardless of whether any substance total is computable. `substance` is `null`. |

#### `incomplete_data`

Silence reads to a user as "you are fine". When the data can't support an
answer, an explicit "we cannot answer this" is the only honest output. There
are two ways the data fails, so there are two triggers.

**Substance-level: known substance, unknown strength.** Excluding a dose of
unknown strength can hold the counted total below the warning fraction. The
duplication detector would then stay silent in precisely the case the product
exists to catch.

- Fires independently of `duplication` / `threshold`. If the counted total
  is already over the warning fraction, both alerts are active.
- Message shape: *"We can't total your acetaminophen because we don't know the
  strength of {product/ingredient}. Your counted total is {N} mg of a {M} mg
  limit, and this figure is incomplete. Check the label or enter the amount."*

**Product-level: unidentified ingredient, no substance at all.** An
`unmatched` ingredient attaches to no substance, so no substance-level alert
can ever fire for it. Example: a cold medicine's acetaminophen appears under a
label string normalization misses, and the user also takes Tylenol. The
duplication detector sums only the Tylenol and stays quiet — total silence in
the exact scenario this product was built for. Spec §5 says normalization
misses are guaranteed, so this is the common case, not the edge.

- `substance` is `null`. **This is the only case in the API where an alert's
  `substance` is `null`.**
- `contributing_doses` is empty; `uncounted_doses` lists the dose(s) of the
  product, each with `unmodeled_reason: "unmatched"` and `ingredient_name` set.
- Message shape: *"We couldn't identify {ingredient string} in {product name},
  so it isn't included in any totals. If you know what it is, enter it
  manually."*

> **Not implemented yet.** `app/detect/alerts.py` produces neither trigger.
> See [Implementation follow-ups](#implementation-follow-ups).

### Alert object

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | required | Alert id. |
| `kind` | string enum | required | `duplication` \| `threshold` \| `accumulation` \| `unmodelable` \| `incomplete_data`. See [Alert kinds](#alert-kinds). |
| `severity` | string enum | required | `info` \| `caution` \| `urgent`. |
| `substance` | `{ id: string, display_name: string }` | nullable **only for product-level `incomplete_data`**; required for every other kind | Same shape as on the curve. **[filled in shape]** |
| `message` | string | required | User-facing text. Must keep the "estimate, not medical advice" framing. **If `uncounted_doses` is non-empty, the message must say the total is incomplete.** |
| `computed_at` | string (ISO 8601 + tz) | required | When the detector produced this alert. |
| `contributing_doses` | ContributingDose[] | required, may be empty | Doses counted in the alert's figure. Never omitted. **Together with `uncounted_doses`, non-empty** (see [Traceability](#traceability)). |
| `uncounted_doses` | UncountedDose[] | required, may be empty | Doses in the alert's window that were **excluded** from the figure. Never omitted. **Together with `contributing_doses`, non-empty.** |
| `notes` | string[] | required, may be empty | Causes behind the alert (population clearance used, threshold lowered for hepatic impairment). Never omitted. |
| `dismissed_at` | string (ISO 8601 + tz) | nullable | When the user dismissed the alert. **Always `null` in v1** — no v1 endpoint sets it. |
| `resolved_at` | string (ISO 8601 + tz) | nullable | When recomputation stopped producing the alert. `null` if still produced. |

#### ContributingDose object

| Field | Type | Required | Description |
|---|---|---|---|
| `dose_event_id` | string | required | Same id returned by `POST /doses`. |
| `product_id` | string | required | |
| `product_name` | string | required | Brand name as shown to the user, so a duplication alert can name the products without a second lookup. |
| `taken_at` | string (ISO 8601 + tz) | required | |
| `amount_mg` | number | required | Mass of **this alert's substance** in this dose, in mg (not the product quantity). For a combination product, only the relevant ingredient. |

#### UncountedDose object

Same shape as ContributingDose, with `amount_mg` always `null` and two added
fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `dose_event_id` | string | required | |
| `product_id` | string | required | |
| `product_name` | string | required | |
| `taken_at` | string (ISO 8601 + tz) | required | |
| `amount_mg` | null | required | Always `null` — the dose couldn't be quantified. |
| `unmodeled_reason` | string enum | required | `unknown_strength` \| `unmatched`. Same values as on [Ingredient](#4-product-search). |
| `ingredient_name` | string | required **[filled in]** | The ingredient as labelled, so the UI can name what's uncounted without a second lookup. Needed for the product-level message. |

The whole point of `contributing_doses` is that a user can see what a number
is built from. A total that hides an omission fails that for the same reason,
so omissions are listed, not footnoted.

```json
[
  {
    "id": "alert_7Q2",
    "kind": "duplication",
    "severity": "urgent",
    "substance": { "id": "acetaminophen", "display_name": "Acetaminophen" },
    "message": "Acetaminophen is in 2 of the products you logged (DayQuil, Tylenol Extra Strength). Your 24-hour total is 4300 mg, which exceeds the 4000 mg limit. Talk to a pharmacist or your prescriber.",
    "computed_at": "2026-09-13T18:02:11-07:00",
    "contributing_doses": [
      { "dose_event_id": "dose_01", "product_id": "prod_tylenol_es_500", "product_name": "Tylenol Extra Strength", "taken_at": "2026-09-13T08:30:00-07:00", "amount_mg": 1000 },
      { "dose_event_id": "dose_02", "product_id": "prod_dayquil_liquicap", "product_name": "DayQuil", "taken_at": "2026-09-13T10:00:00-07:00", "amount_mg": 650 },
      { "dose_event_id": "dose_03", "product_id": "prod_tylenol_es_500", "product_name": "Tylenol Extra Strength", "taken_at": "2026-09-13T12:30:00-07:00", "amount_mg": 1000 },
      { "dose_event_id": "dose_04", "product_id": "prod_dayquil_liquicap", "product_name": "DayQuil", "taken_at": "2026-09-13T14:00:00-07:00", "amount_mg": 650 },
      { "dose_event_id": "dose_05", "product_id": "prod_tylenol_es_500", "product_name": "Tylenol Extra Strength", "taken_at": "2026-09-13T16:30:00-07:00", "amount_mg": 1000 }
    ],
    "uncounted_doses": [],
    "notes": [],
    "dismissed_at": null,
    "resolved_at": null
  },
  {
    "id": "alert_9K4",
    "kind": "incomplete_data",
    "severity": "caution",
    "substance": { "id": "acetaminophen", "display_name": "Acetaminophen" },
    "message": "We can't total your acetaminophen because we don't know the strength of Example Multi-Symptom Cold. Your counted total is 2000 mg of a 4000 mg limit, and this figure is incomplete. Check the label or enter the amount.",
    "computed_at": "2026-09-13T18:02:11-07:00",
    "contributing_doses": [
      { "dose_event_id": "dose_11", "product_id": "prod_tylenol_es_500", "product_name": "Tylenol Extra Strength", "taken_at": "2026-09-13T08:30:00-07:00", "amount_mg": 1000 },
      { "dose_event_id": "dose_12", "product_id": "prod_tylenol_es_500", "product_name": "Tylenol Extra Strength", "taken_at": "2026-09-13T14:30:00-07:00", "amount_mg": 1000 }
    ],
    "uncounted_doses": [
      { "dose_event_id": "dose_13", "product_id": "prod_example_cold_multi", "product_name": "Example Multi-Symptom Cold", "taken_at": "2026-09-13T11:00:00-07:00", "amount_mg": null, "unmodeled_reason": "unknown_strength", "ingredient_name": "ACETAMINOPHEN" }
    ],
    "notes": [],
    "dismissed_at": null,
    "resolved_at": null
  },
  {
    "id": "alert_9K5",
    "kind": "incomplete_data",
    "severity": "caution",
    "substance": null,
    "message": "We couldn't identify PHENYLEPHRINE HCL in Example Multi-Symptom Cold, so it isn't included in any totals. If you know what it is, enter it manually.",
    "computed_at": "2026-09-13T18:02:11-07:00",
    "contributing_doses": [],
    "uncounted_doses": [
      { "dose_event_id": "dose_13", "product_id": "prod_example_cold_multi", "product_name": "Example Multi-Symptom Cold", "taken_at": "2026-09-13T11:00:00-07:00", "amount_mg": null, "unmodeled_reason": "unmatched", "ingredient_name": "PHENYLEPHRINE HCL" }
    ],
    "notes": [],
    "dismissed_at": null,
    "resolved_at": null
  }
]
```

---

## 4. Product search

`GET /products/search`

### Query parameters

| Param | Type | Required | Description |
|---|---|---|---|
| `q` | string | required | Brand, generic, or partial name. Empty `q` → `422`. **[filled in]** |

### Response — `200 OK`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | required | Use as `product_id` when logging. |
| `brand_name` | string | required | |
| `ingredients` | Ingredient[] | required | Active ingredients. **[filled in: may be empty only if label decomposition failed entirely]** |
| `has_unmodeled_ingredients` | boolean | required | `true` if any ingredient has a non-null `unmodeled_reason`. The product is still loggable; those ingredients are excluded from PK and duplication, and the UI must name them. |

Ingredient **[filled in shape]**:

| Field | Type | Required | Description |
|---|---|---|---|
| `substance_id` | string | nullable | Registry id. `null` only when `unmodeled_reason` is `unmatched` — refuse rather than guess. |
| `name` | string | required | Ingredient name as labelled. |
| `strength_mg` | number | nullable | mg per product unit. `null` when the label gives no usable mass (and then `unmodeled_reason` is `unknown_strength` if the substance was recognized). |
| `unmodeled_reason` | string enum | nullable | `unmatched` \| `unknown_strength`. `null` means the ingredient is modeled. If both apply, `unmatched` wins. **[filled in: precedence]** |

```json
[
  {
    "id": "prod_tylenol_es_500",
    "brand_name": "Tylenol Extra Strength",
    "ingredients": [
      { "substance_id": "acetaminophen", "name": "ACETAMINOPHEN", "strength_mg": 500, "unmodeled_reason": null }
    ],
    "has_unmodeled_ingredients": false
  },
  {
    "id": "prod_example_cold_multi",
    "brand_name": "Example Multi-Symptom Cold",
    "ingredients": [
      { "substance_id": "acetaminophen", "name": "ACETAMINOPHEN", "strength_mg": null, "unmodeled_reason": "unknown_strength" },
      { "substance_id": null, "name": "PHENYLEPHRINE HCL", "strength_mg": 5, "unmodeled_reason": "unmatched" }
    ],
    "has_unmodeled_ingredients": true
  }
]
```

---

## 5. Update profile

`PUT /users/{id}/profile`

Full replacement (PUT semantics): omitting an optional field clears it.

### Request

| Field | Type | Required | Description |
|---|---|---|---|
| `age_years` | number | required | > 0. |
| `weight_kg` | number | required | > 0. |
| `sex` | string enum | required | `male` \| `female` (Cockcroft-Gault input; matches `app/pk/individualize.py`). |
| `serum_creatinine_mg_dl` | number | **optional** | mg/dL, > 0. If omitted, population renal clearance is used and curves carry a note saying so. |
| `hepatic_impairment` | string enum | required | `none` \| `mild` \| `moderate` \| `severe`. Lowers thresholds; does **not** apply a numerical clearance correction. |

```json
{
  "age_years": 67,
  "weight_kg": 72.5,
  "sex": "female",
  "serum_creatinine_mg_dl": 1.1,
  "hepatic_impairment": "none"
}
```

### Response — `200 OK`

| Field | Type | Required | Description |
|---|---|---|---|
| `recomputed` | boolean | required | `true` once cached curves and alerts have been recomputed against the new profile. |

```json
{ "recomputed": true }
```

---

## Errors **[filled in]**

The spec does not define errors. Proposed shape for every non-2xx response:

```json
{ "error": { "code": "invalid_timestamp", "message": "taken_at must include a timezone offset" } }
```

| Status | When |
|---|---|
| `404` | Unknown user, product, or substance. |
| `422` | Validation failure (missing field, bad enum, `resolution` not `15m`/`1h`, `active` not `true`/`false`/`all`, timestamp without offset, `to` ≤ `from`, non-positive number). |

An unmodelable substance on `/curve` is **not** an error; it returns `200`
with `modelable: false`.

---

## Decision log

| # | Question | Decision |
|---|---|---|
| 1 | Threshold on `/curve` | Field renamed `threshold_concentration_mg_per_l`, nullable, `null` for every substance at launch. Never derived from a dose threshold. 24h dose totals live on Alerts. Curves screen must work with no threshold line. Registry gains a nullable field of the same name. |
| 2 | Alcohol / unmodelable on `/curve` | `200` with `modelable: false`, a `reason`, and empty `points`. A documented model limit, not a client error. |
| 3 | `resolution` | Whitelist `15m` and `1h`; `422` otherwise. `1h` is decimation of the fixed 15-minute grid; no interpolation (spec §8). |
| 4 | `active` | `true` \| `false` \| `all`, default `true`; `false` returns only inactive alerts. |
| 5 | Alert `notes` | Required array on every alert, may be empty (spec §8: never show a warning without its cause). `payload` is not part of the contract. |
| 6 | Unmatched ingredients | Loggable (spec §5). `substance_id: null`, excluded from PK and duplication, product flagged with `has_unmodeled_ingredients`. Never silently dropped. |
| 7 | Auth | None in v1. Hard constraint: the public deployment runs on seeded synthetic data only; nobody logs real medications into it. |
| 8 | Recognized ingredient, unknown strength | Loggable, unmodeled. `substance_id` resolves, `amount_mg` is `null`, excluded from PK and the 24h duplication sum. Per-ingredient `unmodeled_reason: "unknown_strength"`, distinct from `"unmatched"`. UI names the uncounted ingredient. |
| 9 | What makes an alert inactive | Dismissal (`dismissed_at`) or recomputation no longer producing it (`resolved_at`). No auto-expiry; the recompute decides, not a timer. |
| 10 | Dismissal | Not in v1. `dismissed_at` stays in the schema, always `null`; only recomputation sets an alert inactive. Intended future shape: `POST /users/{id}/alerts/{alert_id}/dismiss`. |
| 11 | Uncounted doses on alerts | Required `uncounted_doses[]` on every alert, same shape as `contributing_doses` with `amount_mg: null` and `unmodeled_reason`. Non-empty → message must say the total is incomplete. |
| 12 | Exclusion silencing an alert | New kind `incomplete_data`, severity `caution`: fires when a substance with `threshold_24h_mg` has any `unknown_strength` dose in the 24h window, regardless of counted total. |
| 13 | Traceability rule | `contributing_doses` and `uncounted_doses` are **together** non-empty for every alert; each alone may be empty. A rule change, not an exemption for one kind. |
| 14 | Unmatched ingredients silencing alerts | `incomplete_data` also fires at the **product level**: any logged product in the 24h window with an `unmatched` ingredient, regardless of whether any substance total is computable. `substance` is `null` on this trigger alone. Normalization misses are guaranteed (spec §5), so this is the common case. |

### Still open

- **Product-level granularity.** If the same product is logged three times in
  the window, is that one `incomplete_data` alert with three `uncounted_doses`,
  or three alerts? And a product with two unmatched ingredients: one alert or
  two? Proposed: one alert per (product, unmatched ingredient string), listing
  every dose of that product in the window. That keeps the message naming one
  ingredient in one product.
- **Product-level severity** is marked **[filled in]** as `caution`, matching
  the substance-level trigger. Confirm.

## Implementation follow-ups

Contract-level decisions that need code. **Write the tests first** (CLAUDE.md).

- **`incomplete_data` detector, substance-level** (`app/detect/alerts.py`).
  Not implemented. Needs: an unmodeled-dose representation alongside
  `LoggedDose`; a detector that fires on substance-with-`threshold_24h_mg` +
  any `unknown_strength` dose in the trailing 24h; `uncounted_doses` populated
  on it and on `duplication` / `threshold`; incomplete-total wording in
  messages when non-empty. Tests to write first, at minimum:
  - counted total **below** the warning fraction + one unknown-strength dose →
    `incomplete_data` fires (the case this kind exists for);
  - counted total **above** it + one unknown-strength dose → both
    `duplication`/`threshold` and `incomplete_data` fire, and the duplication
    message says the total is incomplete;
  - **only** unknown-strength doses, no counted doses → `incomplete_data` fires
    with empty `contributing_doses` and non-empty `uncounted_doses`;
  - no unmodeled doses → no `incomplete_data` (silence on the negative case);
  - unknown-strength dose of a substance with `threshold_24h_mg: null` → no
    `incomplete_data`;
  - unknown-strength dose outside the 24h window → no `incomplete_data`.
- **`incomplete_data` detector, product-level** (`app/detect/alerts.py`).
  Not implemented. Needs a representation of logged products with their
  ingredient strings, since an unmatched ingredient has no `substance_id` to
  hang off `LoggedDose`. Tests to write first, at minimum:
  - the motivating case: Tylenol (matched acetaminophen) + a cold medicine
    whose acetaminophen label string doesn't normalize → duplication stays
    silent on the counted Tylenol total, **and** product-level
    `incomplete_data` fires naming the unmatched string and the product;
  - product-level alert has `substance: null`, empty `contributing_doses`,
    non-empty `uncounted_doses` with `unmodeled_reason: "unmatched"` and
    `ingredient_name`;
  - fires even when no substance in the window has a computable total;
  - **fully matched product → nothing fires** (the negative case);
  - product with an unmatched ingredient logged **outside** the 24h window →
    no `incomplete_data`;
  - product with one matched and one unmatched ingredient → the matched one is
    still counted normally, and the product-level alert names only the
    unmatched one.
- **Traceability invariant** (`Alert` dataclass). Today the dataclass only
  requires `contributing_dose_ids` to be *passed*; nothing checks it's
  non-empty. Add a check that `contributing_dose_ids` and `uncounted_doses` are
  together non-empty, and add a test that constructing
  an alert with both empty fails. Make `substance_id` nullable **only** for
  product-level `incomplete_data` (test: any other kind with no substance
  fails). Add `incomplete_data` to the `kind` comment.

---

*Pharmakon is not a medical device. Outputs are estimates from a
one-compartment, population-average model, for educational and demonstrative
use only.*
