import type {
  Ingredient,
  LoggedIngredient,
  ProductSearchResult,
  ScheduleFrequency,
  ScheduleStatus,
} from "./types/contract";

const PRODUCT_NAME_MAX = 60;

/** Product names from openFDA labels can run to 100+ characters (brand +
 * indication + form + strength + count, all concatenated). Truncate for
 * display; callers show the full name via `title` (hover) or a tap-to-expand
 * control. */
export function truncateProductName(name: string): string {
  return name.length <= PRODUCT_NAME_MAX
    ? name
    : `${name.slice(0, PRODUCT_NAME_MAX - 1).trimEnd()}…`;
}

/** One ingredient as a user should read it, naming why it isn't counted. */
export function ingredientLabel(i: Ingredient): string {
  if (i.unmodeled_reason === "unmatched") {
    return `${i.name} (not recognized, not counted)`;
  }
  if (i.unmodeled_reason === "unknown_strength") {
    return `${i.name} (strength unknown, not counted)`;
  }
  if (i.unmodeled_reason === "no_pk_parameters") {
    return `${i.name} (logged only, no pharmacokinetic data)`;
  }
  return `${i.name} ${i.strength_mg} mg`;
}

/** True if at least one ingredient can be charted (recognized + strength known). */
export function productModelable(p: Pick<ProductSearchResult, "ingredients">): boolean {
  return p.ingredients.some((i) => i.unmodeled_reason === null);
}

/**
 * Why a product with no modelable ingredient still shows no curve. A route
 * of "TOPICAL" is real label data, not a guess -- topical-only substances are
 * out of scope for the PK model (project notes), not unsupported by accident.
 */
export function notModeledReason(route: string | null | undefined): string {
  return route === "TOPICAL"
    ? "Logged — not modeled (topical)"
    : "Logged — not yet in our database";
}

/** Shown on a medication card in place of a current-concentration reading
 * when the substance has no PK parameters at all -- spelled out rather
 * than left as a bare dash, which reads as "no data yet" rather than "this
 * substance can't be charted here." */
export const NOT_MODELED_NO_PK_DATA =
  "Not modeled: no pharmacokinetic data for this substance in our database.";

/** "Demo data, unverified" for the synthetic catalog; the server's own
 * badge text for a curated-skincare or my-product result (see `label` on
 * ProductSearchResult); nothing for real openFDA data. */
const KIND_LABELS: Record<string, string> = {
  medication: "Medication",
  supplement: "Supplement",
  food_drink: "Food & drink",
  skincare: "Skincare",
};

export function kindLabel(p: Pick<ProductSearchResult, "kind">): string | null {
  return p.kind ? (KIND_LABELS[p.kind] ?? null) : null;
}

export function sourceLabel(
  p: Pick<ProductSearchResult, "source" | "label">,
): string | null {
  if (p.source === "catalog") return "Demo data — unverified";
  return p.label;
}

// --- Daily log / timeline ----------------------------------------------------

/** Fixed colors for the suggested categories; freeform categories hash onto
 * a small fixed palette so the same custom category always renders the same
 * color (the point is visual consistency, not a meaning per color). */
const CATEGORY_COLORS: Record<string, string> = {
  skin: "#2563eb",
  sleep: "#7c3aed",
  energy: "#d97706",
  headache: "#dc2626",
  stomach: "#16a34a",
  mood: "#db2777",
  "side effect": "#ea580c",
  pain: "#be123c",
};

const CATEGORY_COLOR_FALLBACK_PALETTE = [
  "#0891b2",
  "#4d7c0f",
  "#9333ea",
  "#0d9488",
  "#b45309",
  "#475569",
];

export function categoryColor(category: string): string {
  const key = category.trim().toLowerCase();
  const known = CATEGORY_COLORS[key];
  if (known) return known;
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return CATEGORY_COLOR_FALLBACK_PALETTE[hash % CATEGORY_COLOR_FALLBACK_PALETTE.length];
}

const VALUE_PLACEHOLDERS: Record<string, string> = {
  skin: "How does your skin look?",
  sleep: "How did you sleep?",
  energy: "How's your energy?",
  headache: "Describe the headache",
  stomach: "How's your stomach?",
  mood: "How are you feeling?",
  "side effect": "Describe what you noticed",
  pain: "Describe the pain",
};

/** Placeholder text for the value input, tailored to the chosen category. */
export function valuePlaceholder(category: string): string {
  return VALUE_PLACEHOLDERS[category.trim().toLowerCase()] ?? "What did you notice?";
}

const BODY_LOCATION_CATEGORIES = new Set(["skin", "pain", "side effect"]);

/** Body location only makes sense for a few categories; hidden otherwise so
 * the form doesn't ask an irrelevant question for e.g. "sleep" or "mood". */
export function showsBodyLocation(category: string): boolean {
  return BODY_LOCATION_CATEGORIES.has(category.trim().toLowerCase());
}

/** A logged dose with no modelable ingredient, shown on the timeline. Same
 * honest framing as notModeledReason, worded for the timeline card. */
export const TIMELINE_DOSE_NOT_MODELED = "Logged — not modeled (topical)";

export function ingredientDisplayName(i: LoggedIngredient): string {
  return i.display_name ?? i.name;
}

/** A logged ingredient as a user should read it: canonical name, its
 * plain-language description in parentheses when the registry has one
 * (never for an unmatched ingredient -- there's no entry to describe it),
 * and why it isn't counted when it isn't. */
export function loggedIngredientLabel(i: LoggedIngredient): string {
  const name = ingredientDisplayName(i);
  const withDescription = i.description ? `${name} (${i.description})` : name;
  if (i.unmodeled_reason === "unmatched") {
    return `${withDescription} (not recognized, not counted)`;
  }
  if (i.unmodeled_reason === "unknown_strength") {
    return `${withDescription} (strength unknown, not counted)`;
  }
  if (i.unmodeled_reason === "no_pk_parameters") {
    return `${withDescription} (logged only, no pharmacokinetic data)`;
  }
  return `${withDescription} ${i.amount_mg} mg`;
}

// --- Medication schedule -------------------------------------------------------

export const FREQUENCY_LABELS: Record<ScheduleFrequency, string> = {
  daily: "Daily",
  twice_daily: "Twice daily",
  three_times_daily: "Three times daily",
  every_other_day: "Every other day",
  weekly: "Weekly",
  as_needed: "As needed",
};

/** Default clock times offered when a frequency is picked. Only a starting
 * point for the time pickers; the user sets the real ones. */
export const DEFAULT_TIMES: Record<ScheduleFrequency, string[]> = {
  daily: ["08:00"],
  twice_daily: ["08:00", "20:00"],
  three_times_daily: ["08:00", "14:00", "20:00"],
  every_other_day: ["08:00"],
  weekly: ["08:00"],
  as_needed: [],
};

export const WEEKDAYS = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
] as const;

export const STATUS_LABELS: Record<ScheduleStatus, string> = {
  taken_today: "Taken",
  due: "Due now",
  overdue: "Overdue",
  upcoming: "Upcoming",
  as_needed: "As needed",
};

/** "Mon, Wed" for a weekly schedule; null when no days were restricted. */
export function daysLabel(days: string[] | null): string | null {
  return days && days.length > 0
    ? days.map((d) => d[0].toUpperCase() + d.slice(1, 3)).join(", ")
    : null;
}

/** "8:00 AM" from "08:00". */
export function timeOfDayLabel(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}
