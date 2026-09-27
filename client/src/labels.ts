import type { Ingredient } from "./types/contract";

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
