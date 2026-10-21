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

/**
 * "mg/L" for a concentration curve. "mg" only on an approximate curve
 * (SubstanceCurve.fidelity): the amount left in the body, drawn when the
 * registry has a half-life and bioavailability but no volume of distribution.
 */
export type ConcentrationUnit = "mg/L" | "mg";

export interface SubstanceRef {
  id: string; // registry id, e.g. "acetaminophen"
  display_name: string;
  // Plain-language "what this is / does", from the registry field of the
  // same name. null unless the registry documents one -- never invented.
  description: string | null;
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
  // "approximate" is a simplified decay curve in mg (see ConcentrationUnit);
  // its `notes` say what is missing.
  fidelity: "full" | "approximate" | null;
  points: ConcentrationPoint[]; // ordered by t ascending; empty if !modelable
  // Published plasma concentration from the registry. Never derived from a
  // dose threshold. null for every substance at launch — the Curves screen
  // must render correctly with no threshold line.
  threshold_concentration_mg_per_l: number | null;
  peak: ConcentrationPoint | null;
  current: ConcentrationPoint | null;
  // The same curve with future scheduled doses added, from now on. null when
  // no schedule contributes. A projection, never part of `points`.
  projected_points: ConcentrationPoint[] | null;
  // Causes behind the curve (e.g. population clearance used because no serum
  // creatinine is on file). May be empty, never omitted.
  notes: string[];
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

// "no_pk_parameters": recognized, but the registry has too few pharmacokinetic
// parameters to chart it. Logged only, like the other two.
export type UnmodeledReason =
  | "unmatched"
  | "unknown_strength"
  | "no_pk_parameters";

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
/** Substance as carried on an Alert -- adds threshold_explanation, which
 * /curve's SubstanceRef does not have. */
export interface AlertSubstanceRef extends SubstanceRef {
  // Why threshold_24h_mg matters, from the registry field of the same name.
  // null unless the registry documents one -- never derived or invented.
  // Shown as "Why this matters" next to the alert.
  threshold_explanation: string | null;
  verified: boolean;
}

export interface Alert {
  id: string;
  kind: AlertKind;
  severity: AlertSeverity;
  // null ONLY for product-level incomplete_data. A null substance does NOT
  // imply contributing_doses is empty (escalated product-level alerts).
  substance: AlertSubstanceRef | null;
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

/** "catalog" is the synthetic seed catalog (unverified strengths); "openfda"
 * is a real openFDA drug label. The UI must mark "catalog" results distinctly
 * so a fabricated-looking strength is never mistaken for verified data. */
/** "user_entered" is a product the user pasted an ingredient list for
 * (POST /products/user-entered) -- for cosmetics openFDA has no label for. */
export type ProductSource = "catalog" | "openfda" | "user_entered";

export interface ProductSearchResult {
  id: string; // use as product_id when logging
  brand_name: string;
  ingredients: Ingredient[];
  has_unmodeled_ingredients: boolean;
  source: ProductSource;
  // As labelled by openFDA (e.g. "TOPICAL", "ORAL"); null for catalog
  // products (route isn't tracked there) or when openFDA didn't report one.
  route: string | null;
}

/** `results` is capped at 10; `total` is the full count before that cap, so
 * the client can show "Showing X of Y results" and rely on continued typing
 * to narrow the query rather than paginating. */
export interface ProductSearchResponse {
  results: ProductSearchResult[];
  total: number;
}

// ---------------------------------------------------------------------------
// GET /users/{id}/doses  (read-only list; see docs/api-contract.md)
// ---------------------------------------------------------------------------

export interface LoggedIngredient {
  name: string; // as labelled
  substance_id: string | null;
  // Registry display name and plain-language description, both null when
  // substance_id is null (unmatched -- no registry entry to pull from).
  display_name: string | null;
  description: string | null;
  amount_mg: number | null; // null when unmodeled
  unmodeled_reason: UnmodeledReason | null;
}

export interface LoggedDoseEvent {
  dose_event_id: string;
  product_id: string;
  product_name: string;
  taken_at: IsoTimestamp;
  quantity: number;
  ingredients: LoggedIngredient[];
}

// ---------------------------------------------------------------------------
// POST /symptom-check  (see docs/api-contract.md, section 7)
// ---------------------------------------------------------------------------

export interface SymptomCheckRequest {
  symptom: string;
}

export type AssociationSource = "openfda_label" | "local_reference";

/**
 * An association, never a cause: the symptom is listed for a substance the
 * user has at or above 75% of its limit.
 */
export interface SymptomMatch {
  substance: SubstanceRef;
  symptom: string;
  source: AssociationSource;
  source_label: string; // human-readable source, e.g. "FDA drug label"
  verified: false;
  current_level_mg_l: number | null; // null when the substance can't be modeled
  total_24h_mg: number;
  threshold_24h_mg: number | null;
  threshold_status: "above" | "approaching";
  contributing_doses: ContributingDose[];
  message: string;
}

export interface SymptomCheckResponse {
  matches: SymptomMatch[]; // empty when nothing qualifies
  message: string;
  unchecked_reason?: string[];
}

// ---------------------------------------------------------------------------
// GET /users/{id}/side-effect-profile  (see app/api/side_effects.py)
// ---------------------------------------------------------------------------

export interface ContributingProduct {
  product_id: string;
  product_name: string;
}

export interface SideEffectEntry {
  effect: string;
  source: AssociationSource;
  source_label: string;
  verified: false;
}

export interface SubstanceSideEffectProfile {
  substance: SubstanceRef;
  current_level_mg_l: number | null; // null when the substance can't be modeled
  total_24h_mg: number;
  threshold_24h_mg: number | null;
  side_effects: SideEffectEntry[];
  contributing_products: ContributingProduct[];
}

/** Every documented side effect of every substance logged in the last 24
 * hours, grouped by substance. `shared_effects` names effects documented for
 * more than one logged substance -- these compound and are worth calling out. */
export interface SideEffectProfileResponse {
  substances: SubstanceSideEffectProfile[];
  shared_effects: string[];
  unchecked_reason?: string[];
}

// ---------------------------------------------------------------------------
// Skincare ingredient interaction checker (see app/api/skincare.py)
//
// NOT pharmacokinetics: no curves, no concentrations, no risk scores. A
// lookup against documented dermatological guidelines, every entry cited.
// ---------------------------------------------------------------------------

export interface SkincareProductRequest {
  name: string;
  ingredients: string[];
}

export interface SkincareCheckedProduct {
  name: string;
  ingredients: string[];
}

export interface SkincareIncompatibility {
  ingredient_a: string;
  product_a: string;
  // matched_ingredient_a duplicates ingredient_a -- present so the UI can
  // pair "which string" with "which rule term" (matched_alias_a) without
  // knowing whether it's reading the a-side or b-side of a finding.
  matched_ingredient_a: string;
  matched_alias_a: string; // the rule's canonical name or alias that hit
  ingredient_b: string;
  product_b: string;
  matched_ingredient_b: string;
  matched_alias_b: string;
  effect: string;
  // Rewritten when product_a === product_b: the source's timing advice
  // ("use at different times") doesn't apply when both ingredients are
  // already combined in one product.
  recommendation: string;
  source: string;
}

export interface SkincareIrritant {
  ingredient: string;
  product: string;
  matched_ingredient: string; // duplicates ingredient; see SkincareIncompatibility
  matched_alias: string; // the rule's canonical name or alias that hit
  effect: string;
  source: string;
  note?: string; // added only when relevant to something else the user logged
}

export interface SkincareClearIngredient {
  ingredient: string;
  product: string;
}

export interface SkincareCheckResponse {
  products: SkincareCheckedProduct[];
  incompatibilities: SkincareIncompatibility[];
  irritants: SkincareIrritant[];
  clear: SkincareClearIngredient[];
  // Total documented rules checked (incompatibilities + irritants), not how
  // many matched -- lets the UI say "no matching rule among our M
  // documented rules" rather than implying a guarantee of safety.
  rules_checked: number;
}

// ---------------------------------------------------------------------------
// Skincare alternatives (see app/api/alternatives.py)
//
// Curated only -- app/data/skincare_products.json is hand-sourced, never
// generated or fetched live. Never a recommendation: only what was checked.
// ---------------------------------------------------------------------------

export interface SkincareAlternativeCandidate {
  brand: string;
  name: string;
  category: string;
  ingredients: string[]; // full list, straight from the curated source
  source_url: string;
  date_checked: string;
  verified: boolean;
  checked_note: string; // e.g. "Does not contain Retinol. No documented..."
  // Set only when this group's flagged_ingredient was one half of an
  // incompatibility entirely within the user's own product (see
  // SkincareAlternativesGroup.same_product_partners): what the candidate's
  // own ingredient list says about the OTHER half. Never a safety claim.
  same_product_note: string | null;
  search_url: string; // Google Shopping search, brand + name, no tracking
}

/** Alternatives for one flagged ingredient of one product. */
export interface SkincareAlternativesGroup {
  flagged_ingredient: string;
  product_name: string;
  // Only set when the flagged product itself happens to be a curated one --
  // never guessed otherwise, so alternatives then span every category.
  category: string | null;
  // Non-empty only when flagged_ingredient was one half of an
  // incompatibility entirely within this same product -- the other
  // ingredient(s) of that pair. Drives the "replacing this product removes
  // both ingredients" banner.
  same_product_partners: string[];
  alternatives: SkincareAlternativeCandidate[]; // at most 5
}

export interface SkincareAlternativesResponse {
  results: SkincareAlternativesGroup[];
  disclaimer: string;
}

// ---------------------------------------------------------------------------
// Medication interaction checker (see app/api/interactions.py)
//
// Informational only -- never creates or feeds an Alert. Two cited sources:
// curated rules (verified: false until a human opens the source) and live
// openFDA drug_interactions label text (quoted verbatim, never paraphrased).
// ---------------------------------------------------------------------------

export interface InteractionCheckRequest {
  names: string[]; // product or substance names, as typed
}

export type QuoteSource = "fda_label_verbatim";

export type InteractionSourceType = "curated" | "fda_label";

export interface InteractionSubstanceSource {
  kind: "dose" | "schedule" | "manual";
  label: string; // product name, or the raw text typed for a manual check
  dose_event_id: string | null;
  product_id: string | null;
  schedule_id: string | null;
  taken_at: IsoTimestamp | null;
}

export interface InteractionSubstanceRef {
  id: string;
  display_name: string;
  description: string | null;
  sources: InteractionSubstanceSource[];
}

/** A window this estimate says both substances are above 10% of their own
 * peak -- computed from the concentration model, always labelled as such. */
export interface OverlapWindow {
  from: IsoTimestamp;
  to: IsoTimestamp;
  estimate: true;
  description: string;
}

export interface Interaction {
  substance_a: InteractionSubstanceRef;
  substance_b: InteractionSubstanceRef;
  effect: string | null; // curated only
  recommendation: string | null; // curated only
  source_type: InteractionSourceType;
  source: string; // citation (curated) or "FDA label, <drug>, ..." (fda_label)
  quoted_text: string | null; // fda_label only, <=300 chars, verbatim
  quote_source: QuoteSource | null;
  verified: boolean;
  severity: string | null; // null unless the source itself states one
  overlap: OverlapWindow[] | null; // null when either substance isn't modeled
  message: string;
}

export interface InteractionCheckResponse {
  interactions: Interaction[];
  checked_pairs: number;
  // Never silently dropped: unresolved ingredient/substance names and an
  // unreachable openFDA are both surfaced here.
  unchecked_reason: string[];
  // Set to the exact required sentence when `interactions` is empty, else null.
  message: string | null;
}

// ---------------------------------------------------------------------------
// Food, drink, and timing guidance (see app/api/food_guidance.py)
//
// Two cited sources: live openFDA label text (quoted verbatim, <=300 chars)
// and a curated quote list (verified: false until a human opens the
// source). Oral medications only -- a non-oral or undeclared route
// produces no guidance, surfaced in unchecked_reason instead. Never
// creates or feeds an alert.
// ---------------------------------------------------------------------------

export interface FoodGuidanceRequest {
  names: string[]; // medication names, as typed
}

export type FoodGuidanceClass =
  | "take_with_food"
  | "take_on_empty_stomach"
  | "avoid_item"
  | "separate_from"
  | "other";

export type FoodGuidanceSourceType = "fda_label" | "curated";

/** Only set when the quote states explicit hours ("2 hours before or 4
 * hours after") -- never derived or guessed otherwise. */
export interface FoodGuidanceWindow {
  before_hours: number;
  after_hours: number;
}

export interface FoodGuidanceFinding {
  name: string; // substance display name, or the product name for a combo
  is_product: boolean; // true when `name` is a combination product
  item: string; // e.g. "food", "grapefruit", "vitamin k"
  class: FoodGuidanceClass;
  // <=300 chars, verbatim from the label -- only when the underlying
  // sentence passed the quote-integrity rules (true sentence start/end, no
  // heading/cross-reference noise, under the length cap). null means the
  // label mentions this item but couldn't be safely quoted; see `pointer`.
  quote: string | null;
  // Set only when `quote` is null: a citation sentence (never a quote)
  // pointing to the label section, with a DailyMed link when a set_id was
  // available. class is always "other" alongside a pointer finding.
  pointer: string | null;
  source_type: FoodGuidanceSourceType;
  label_field: string; // e.g. "dosage_and_administration"
  quote_source: QuoteSource | null;
  verified: boolean;
  window: FoodGuidanceWindow | null;
  // True when a curated entry quoted the same sentence (exact, or after
  // whitespace normalization) -- that entry is folded into this one finding
  // rather than shown twice. Always false on a source_type "curated" finding.
  also_in_curated: boolean;
  // The label this finding was read from -- read off the label's own text,
  // null when nothing matched (never guessed). A form-specific instruction
  // (e.g. "extended-release tablet") should never be shown as if it applied
  // generally.
  product_name: string | null;
  dosage_form: string | null;
}

export type FoodGuidanceTimelineType = "dose_marker" | "window" | "ongoing";

export interface FoodGuidanceTimelineEntry {
  type: FoodGuidanceTimelineType;
  class: FoodGuidanceClass;
  name: string;
  quote: string;
  at?: IsoTimestamp; // dose_marker only
  source?: "scheduled" | "logged"; // dose_marker only
  from?: IsoTimestamp; // window only
  to?: IsoTimestamp; // window only
  note?: string; // ongoing only
}

export interface FoodGuidanceResponse {
  guidance: FoodGuidanceFinding[];
  timeline: FoodGuidanceTimelineEntry[];
  // Never silently dropped: unrecognized names, non-oral/unknown routes,
  // and an unreachable openFDA are all surfaced here.
  unchecked_reason: string[];
  // Set to the exact required sentence when `guidance` is empty, else null.
  message: string | null;
}

// ---------------------------------------------------------------------------
// Daily log: POST/GET/DELETE /users/{id}/log  (see app/api/logger.py)
// ---------------------------------------------------------------------------

/** UI hint only -- category is freeform text, not an enum the server
 * enforces. A dropdown should offer these plus a "custom" option. */
export const SUGGESTED_LOG_CATEGORIES = [
  "skin",
  "sleep",
  "energy",
  "headache",
  "stomach",
  "mood",
  "side effect",
  "pain",
] as const;

export interface DailyLogRequest {
  category: string; // freeform, non-empty
  value: string; // freeform, non-empty
  body_location?: string;
  notes?: string;
  logged_at?: IsoTimestamp; // when it happened, not when logged; default now
}

export interface DailyLogEntry {
  id: string;
  category: string;
  value: string;
  body_location: string | null;
  notes: string | null;
  logged_at: IsoTimestamp;
  created_at: IsoTimestamp;
}

// ---------------------------------------------------------------------------
// GET /users/{id}/timeline  (doses + daily log entries, one merged axis)
// ---------------------------------------------------------------------------

export interface TimelineQuery {
  from?: IsoTimestamp;
  to?: IsoTimestamp;
}

export interface TimelineDoseEntry {
  type: "dose";
  timestamp: IsoTimestamp;
  id: string; // same as dose_event_id
  product_id: string;
  product_name: string;
  quantity: number;
  ingredients: LoggedIngredient[];
}

export interface TimelineLogEntry {
  type: "log";
  timestamp: IsoTimestamp;
  id: string; // same as the daily log entry's id
  category: string;
  value: string;
  body_location: string | null;
  notes: string | null;
}

/** A slot from the medication schedule whose outcome is settled. Not a dose:
 * a "taken" slot points at the dose that satisfied it. */
export interface TimelineScheduledEntry {
  type: "scheduled";
  timestamp: IsoTimestamp; // when the dose was scheduled for
  id: string;
  schedule_id: string;
  product_id: string;
  product_name: string;
  status: "taken" | "missed";
  dose_event_id: string | null;
}

/** Chronological record only -- never a correlation or a causal claim. */
export type TimelineEntry =
  | TimelineDoseEntry
  | TimelineLogEntry
  | TimelineScheduledEntry;

// ---------------------------------------------------------------------------
// Medication schedule: /users/{id}/schedule  (see app/api/schedule.py)
// ---------------------------------------------------------------------------

export type ScheduleFrequency =
  | "daily"
  | "twice_daily"
  | "three_times_daily"
  | "every_other_day"
  | "weekly"
  | "as_needed";

/** as_needed is never due, so it has a status of its own. */
export type ScheduleStatus =
  | "due"
  | "upcoming"
  | "overdue"
  | "taken_today"
  | "as_needed";

export type SlotStatus = "upcoming" | "due" | "overdue" | "taken" | "missed";

export interface ScheduleRequest {
  product_id: string;
  frequency: ScheduleFrequency;
  times: string[]; // "HH:MM", local to `timezone`
  days_of_week?: string[]; // lowercase English names
  notes?: string;
  timezone?: string; // IANA name
}

export interface ScheduledSubstance {
  substance_id: string | null;
  display_name: string | null;
  name: string; // as labelled
  amount_mg: number | null; // per unit
  unmodeled_reason: UnmodeledReason | null;
  current_concentration: number | null; // mg/L; null when not modeled
  unit: ConcentrationUnit;
}

export interface ScheduledSlot {
  scheduled_at: IsoTimestamp;
  status: SlotStatus;
  taken_at: IsoTimestamp | null;
  dose_event_id: string | null;
}

export interface ScheduledMedication {
  id: string;
  product_id: string;
  product_name: string;
  frequency: ScheduleFrequency;
  times: string[];
  days_of_week: string[] | null;
  timezone: string | null;
  notes: string | null;
  active: boolean;
  created_at: IsoTimestamp;
  status: ScheduleStatus;
  next_due: IsoTimestamp | null; // may be in the past when overdue
  last_taken: IsoTimestamp | null; // from logged doses, however they were logged
  today: ScheduledSlot[];
  substances: ScheduledSubstance[];
}

export interface TakeScheduledResponse {
  dose_event_id: string;
  schedule_id: string;
  triggered_alerts: Alert[];
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

/** A product the user has logged before, for the search's Recent list. */
export interface RecentProduct {
  product_id: string;
  product_name: string;
}
