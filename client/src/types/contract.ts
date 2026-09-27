// Wire types for the Pharmakon API. Source of truth: docs/api-contract.md.
// If this file and the contract disagree, the contract wins — fix this file.
//
// Notation (from the contract):
//   required  -> plain field, never null
//   nullable  -> `T | null`, always present in responses
//   optional  -> `?`, may be omitted from requests
//
// All timestamps are ISO 8601 strings WITH a timezone offset, e.g.
// "2026-09-13T08:30:00-07:00". The server rejects timestamps without one.
// All concentrations are mg/L. All masses are mg.

/** ISO 8601 timestamp with timezone offset. */
export type IsoTimestamp = string;

/** The only concentration unit in this API. */
export type ConcentrationUnit = "mg/L";

export interface SubstanceRef {
  id: string; // registry id, e.g. "acetaminophen"
  display_name: string;
}

// ---------------------------------------------------------------------------
// 1. POST /users/{id}/doses
// ---------------------------------------------------------------------------

export interface DoseRequest {
  product_id: string;
  taken_at: IsoTimestamp; // when taken, not when logged
  quantity: number; // count of product units, > 0
}

/** 201 Created */
export interface DoseResponse {
  dose_event_id: string;
  triggered_alerts: Alert[]; // empty if none, never null
}

// ---------------------------------------------------------------------------
// 2. GET /users/{id}/curve
// ---------------------------------------------------------------------------

export type CurveResolution = "15m" | "1h";

export interface CurveQuery {
  substance_id: string;
  from: IsoTimestamp; // inclusive; URL-encode the "+" in offsets
  to: IsoTimestamp; // inclusive; must be after `from`
  resolution?: CurveResolution; // default "15m"
}

export interface ConcentrationPoint {
  t: IsoTimestamp;
  concentration: number; // mg/L, >= 0
}

/**
 * Response of GET /curve. A substance the model can't represent (e.g.
 * zero-order alcohol) is still 200, with modelable=false, a reason, and
 * empty points.
 */
export interface SubstanceCurve {
  substance: SubstanceRef;
  modelable: boolean;
  reason: string | null; // null when modelable is true
  unit: ConcentrationUnit;
  points: ConcentrationPoint[]; // ordered by t ascending; empty if !modelable
  // Published plasma concentration from the registry. Never derived from a
  // dose threshold. null for every substance at launch — the Curves screen
  // must render correctly with no threshold line.
  threshold_concentration_mg_per_l: number | null;
  peak: ConcentrationPoint | null;
  current: ConcentrationPoint | null;
}

// ---------------------------------------------------------------------------
// 3. GET /users/{id}/alerts
// ---------------------------------------------------------------------------

/** `false` returns ONLY inactive alerts; `all` returns both. Default `true`. */
export type AlertsActiveFilter = "true" | "false" | "all";

export type AlertKind =
  | "duplication"
  | "threshold"
  | "accumulation"
  | "unmodelable"
  | "incomplete_data";

export type AlertSeverity = "info" | "caution" | "urgent";

export type UnmodeledReason = "unmatched" | "unknown_strength";

/** A dose counted in the alert's figure. */
export interface ContributingDose {
  dose_event_id: string;
  product_id: string;
  product_name: string;
  taken_at: IsoTimestamp;
  amount_mg: number; // mass of THIS alert's substance in the dose
}

/** A dose in the alert's window that was excluded from the figure. */
export interface UncountedDose {
  dose_event_id: string;
  product_id: string;
  product_name: string;
  taken_at: IsoTimestamp;
  amount_mg: null; // always null — the dose couldn't be quantified
  ingredient_name: string; // as labelled
  unmodeled_reason: UnmodeledReason;
}

/**
 * Traceability: contributing_doses and uncounted_doses are TOGETHER
 * non-empty on every alert. Either alone may be empty.
 *
 * Active iff dismissed_at and resolved_at are both null. dismissed_at is
 * always null in v1 (no dismissal endpoint).
 */
export interface Alert {
  id: string;
  kind: AlertKind;
  severity: AlertSeverity;
  // null ONLY for product-level incomplete_data. A null substance does NOT
  // imply contributing_doses is empty (escalated product-level alerts).
  substance: SubstanceRef | null;
  message: string;
  notes: string[]; // causes behind the alert; may be empty, never omitted
  computed_at: IsoTimestamp;
  contributing_doses: ContributingDose[];
  uncounted_doses: UncountedDose[];
  dismissed_at: IsoTimestamp | null;
  resolved_at: IsoTimestamp | null;
}

// ---------------------------------------------------------------------------
// 4. GET /products/search?q=
// ---------------------------------------------------------------------------

export interface Ingredient {
  substance_id: string | null; // null only when unmodeled_reason is "unmatched"
  name: string; // as labelled
  strength_mg: number | null; // mg per product unit
  unmodeled_reason: UnmodeledReason | null; // null = modeled
}

export interface ProductSearchResult {
  id: string; // use as product_id when logging
  brand_name: string;
  ingredients: Ingredient[];
  has_unmodeled_ingredients: boolean;
}

// ---------------------------------------------------------------------------
// 5. PUT /users/{id}/profile  (full replacement)
// ---------------------------------------------------------------------------

export type Sex = "male" | "female";
export type HepaticImpairment = "none" | "mild" | "moderate" | "severe";

export interface ProfileUpdate {
  age_years: number; // > 0
  weight_kg: number; // > 0
  sex: Sex;
  serum_creatinine_mg_dl?: number; // mg/dL, > 0; omit -> population clearance
  hepatic_impairment: HepaticImpairment;
}

export interface ProfileResponse {
  recomputed: boolean;
}

// ---------------------------------------------------------------------------
// Errors (every non-2xx response)
// ---------------------------------------------------------------------------

export interface ApiError {
  error: { code: string; message: string };
}

// ---------------------------------------------------------------------------
// Client-side aggregate — NOT a contract response.
// ---------------------------------------------------------------------------

/**
 * Convenience bundle for the client (and its mock data). The API has no
 * endpoint returning this shape; curves come one per GET /curve, alerts from
 * GET /alerts.
 */
export interface EngineResponse {
  unit: ConcentrationUnit;
  curves: SubstanceCurve[];
  alerts: Alert[];
}
