import type {
  Ingredient,
  LoggedIngredient,
  ProductSearchResult,
} from "./types/contract";

/** One ingredient as a user should read it, naming why it isn't counted. */
export function ingredientLabel(i: Ingredient): string {
  if (i.unmodeled_reason === "unmatched") {
    return `${i.name} (not recognized, not counted)`;
  }
  if (i.unmodeled_reason === "unknown_strength") {
    return `${i.name} (strength unknown, not counted)`;
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
 * out of scope for the PK model (CLAUDE.md), not unsupported by accident.
 */
export function notModeledReason(route: string | null | undefined): string {
  return route === "TOPICAL"
    ? "Logged — not modeled (topical product)"
    : "Logged — not yet in our database";
}

/** "Demo data, unverified" for the synthetic catalog; nothing for real openFDA data. */
export function sourceLabel(p: Pick<ProductSearchResult, "source">): string | null {
  return p.source === "catalog" ? "Demo data — unverified" : null;
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
export const TIMELINE_DOSE_NOT_MODELED = "Logged — not modeled (topical/unmatched)";

/** Title Case for a raw openFDA label string (e.g. "GLYCERIN" -> "Glycerin").
 * Used only when there's no registry match -- ALL CAPS reads as shouting,
 * but there's no canonical registry name to show instead. */
export function titleCaseIngredient(raw: string): string {
  return raw
    .toLowerCase()
    .split(" ")
    .map((w) => (w.length === 0 ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

/** The name to show for a logged ingredient: the registry's canonical name
 * when resolved, else the raw label title-cased. */
export function ingredientDisplayName(i: LoggedIngredient): string {
  return i.display_name ?? titleCaseIngredient(i.name);
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
  return `${withDescription} ${i.amount_mg} mg`;
}
