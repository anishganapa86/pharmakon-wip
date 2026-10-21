import { ingredientDisplayName } from "../labels";
import type { LoggedIngredient } from "../types/contract";

export function TopicalIngredients({
  ingredients,
}: {
  ingredients: LoggedIngredient[];
}) {
  if (ingredients.length === 0) return null;
  return (
    <details className="collapsible">
      <summary>Ingredients ({ingredients.length})</summary>
      <p className="muted small collapsible-body">
        {ingredients.map(ingredientDisplayName).join(", ")}
      </p>
    </details>
  );
}
