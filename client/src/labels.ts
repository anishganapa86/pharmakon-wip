import type { Ingredient, ProductSearchResult } from "./types/contract";

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
