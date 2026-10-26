import { useId, useState } from "react";

const PREVIEW_COUNT = 4;

export function IngredientPreview({ ingredients }: { ingredients: string[] }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const hasMore = ingredients.length > PREVIEW_COUNT;
  const shown = open ? ingredients : ingredients.slice(0, PREVIEW_COUNT);
  return (
    <div className="ingredient-preview">
      <p className="muted small ingredient-names" id={listId}>
        {shown.join(", ")}
        {hasMore && !open ? ", …" : ""}
      </p>
      {hasMore && (
        <button
          type="button"
          className="button-link ingredient-toggle"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen(!open)}
        >
          {open ? "Show fewer ingredients" : "Show all ingredients"}
        </button>
      )}
    </div>
  );
}
