import { useEffect, useId, useRef, useState } from "react";
import { createMyProduct, searchProducts } from "../api";
import { splitIngredientsPreview } from "../ingredientSplit";
import {
  ingredientLabel,
  productModelable,
  kindLabel,
  sourceLabel,
  truncateProductName,
} from "../labels";
import type {
  MyProductCategory,
  ProductSearchResult,
  RecentProduct,
} from "../types/contract";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const DEBOUNCE_MS = 200;

const CATEGORY_OPTIONS: MyProductCategory[] = [
  "cleanser",
  "toner",
  "serum",
  "moisturizer",
  "sunscreen",
  "other",
];

const EMPTY_ADD_DRAFT = {
  brand: "",
  name: "",
  category: "other" as MyProductCategory,
  rawText: "",
};

interface Props {
  selected: ProductSearchResult | null;
  onSelect: (product: ProductSearchResult | null) => void;
  recent: RecentProduct[];
  // Both optional, and only together: when provided, a search with no
  // results offers "Add this product with its ingredient list", saving to
  // the user's own library and calling onSavedProductLogged right away.
  // When either is omitted (e.g. the medication schedule's product pick),
  // that option is hidden entirely and a no-results search just says so --
  // this is a plain search pick, with no "log it right away" to make sense of.
  userId?: string;
  onSavedProductLogged?: (product: ProductSearchResult) => void;
}

export function ProductSearch({
  selected,
  onSelect,
  recent,
  userId,
  onSavedProductLogged,
}: Props) {
  const canAddProduct = userId !== undefined && onSavedProductLogged !== undefined;
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ProductSearchResult[]>([]);
  const [total, setTotal] = useState(0);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [recentLoadingId, setRecentLoadingId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addDraft, setAddDraft] = useState(EMPTY_ADD_DRAFT);
  const [addSaving, setAddSaving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  // Debounced search. State is only set from the timer and promise callbacks.
  useEffect(() => {
    const q = query.trim();
    if (!q || selected) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setSearching(true);
      searchProducts(q, controller.signal)
        .then((r) => {
          setResults(r.results);
          setTotal(r.total);
          setActive(0);
          setError(null);
        })
        .catch((e: unknown) => {
          if (controller.signal.aborted) return;
          setResults([]);
          setTotal(0);
          setError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, selected]);

  // Clicking anywhere outside the search closes the dropdown.
  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, []);

  function handleChange(value: string) {
    setQuery(value);
    setOpen(true);
    if (selected) onSelect(null);
    if (!value.trim()) {
      setResults([]);
      setError(null);
    }
  }

  function choose(product: ProductSearchResult) {
    onSelect(product);
    setQuery(product.brand_name);
    setOpen(false);
  }

  // Recent entries only carry an id and a name (from the dose log), so
  // re-run the search to get current ingredients/strengths before selecting.
  async function chooseRecent(r: RecentProduct) {
    setRecentLoadingId(r.product_id);
    try {
      const found = (await searchProducts(r.product_name)).results;
      const match = found.find((p) => p.id === r.product_id) ?? found[0];
      if (match) choose(match);
    } catch {
      // Best effort; leave the search box as-is on failure.
    } finally {
      setRecentLoadingId(null);
    }
  }

  function clear() {
    onSelect(null);
    setQuery("");
    setResults([]);
    setTotal(0);
    setError(null);
    setOpen(false);
    setAddOpen(false);
    setAddDraft(EMPTY_ADD_DRAFT);
    setAddError(null);
  }

  const addPreview = splitIngredientsPreview(addDraft.rawText);

  function openAddForm() {
    setAddDraft((d) => ({ ...d, name: d.name || query.trim() }));
    setAddOpen(true);
  }

  async function saveAndLogProduct() {
    if (!userId || !onSavedProductLogged) return;
    if (!addDraft.brand.trim() || !addDraft.name.trim() || addPreview.length === 0) {
      return;
    }
    setAddSaving(true);
    setAddError(null);
    try {
      const entry = await createMyProduct(userId, {
        brand: addDraft.brand.trim(),
        name: addDraft.name.trim(),
        category: addDraft.category,
        raw_text: addDraft.rawText,
      });
      // Re-search to get the product shaped exactly like any other search
      // result (decomposed ingredients, labels), the same pattern
      // chooseRecent uses for a recent product.
      const found = (await searchProducts(`${entry.brand} ${entry.name}`)).results;
      const product = found.find((p) => p.id === entry.id);
      setAddOpen(false);
      setAddDraft(EMPTY_ADD_DRAFT);
      if (product) {
        setOpen(false);
        setQuery("");
        onSavedProductLogged(product);
      } else {
        setOpen(true);
        setQuery(`${entry.brand} ${entry.name}`);
        setAddError(
          "Saved to My products, but it didn't come back in search, so it " +
            "wasn't logged. Search for it by name to log it.",
        );
      }
    } catch (err) {
      setAddError(errorMessage(err));
    } finally {
      setAddSaving(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "ArrowDown" && results.length > 0) {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, results.length - 1));
    } else if (e.key === "ArrowUp" && results.length > 0) {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter" && open && results[active]) {
      e.preventDefault();
      choose(results[active]);
    }
  }

  const showDropdown = open && !selected && query.trim() !== "";

  const addControl = !addOpen ? (
    <div>
      {addError && <p className="error small">{addError}</p>}
      <button type="button" className="button-link" onClick={() => openAddForm()}>
        Add this product with its ingredient list
      </button>
    </div>
  ) : (
    <div
      className="manual-entry"
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") {
          e.preventDefault();
          void saveAndLogProduct();
        }
      }}
    >
      <p className="muted small">
        Most cosmetics aren't FDA-regulated drugs, so we don't have a label for
        this. Enter it once and it's saved to your own product library.
      </p>
      <input
        className="control"
        type="text"
        placeholder="Brand (e.g. TIRTIR)"
        value={addDraft.brand}
        onChange={(e) => setAddDraft((d) => ({ ...d, brand: e.target.value }))}
        aria-label="Brand"
      />
      <input
        className="control"
        type="text"
        placeholder="Product name (e.g. Milk Skin Toner)"
        value={addDraft.name}
        onChange={(e) => setAddDraft((d) => ({ ...d, name: e.target.value }))}
        aria-label="Product name"
      />
      <select
        className="control"
        value={addDraft.category}
        onChange={(e) =>
          setAddDraft((d) => ({
            ...d,
            category: e.target.value as MyProductCategory,
          }))
        }
        aria-label="Category"
      >
        {CATEGORY_OPTIONS.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      <textarea
        className="control manual-entry-textarea"
        placeholder="Paste the ingredient list from the packaging"
        value={addDraft.rawText}
        onChange={(e) => setAddDraft((d) => ({ ...d, rawText: e.target.value }))}
        aria-label="Ingredients"
        rows={4}
      />
      <p className="muted small">
        {addDraft.rawText.trim() === ""
          ? "No ingredients yet."
          : addPreview.length === 0
            ? "Couldn't find any ingredients in that text."
            : `Parsed ${addPreview.length} ingredient${
                addPreview.length === 1 ? "" : "s"
              }: ${addPreview.slice(0, 3).join(", ")}${
                addPreview.length > 3 ? ", …" : ""
              }. If that looks wrong, edit the text above.`}
      </p>
      {addError && <p className="error small">Couldn't save: {addError}</p>}
      <button
        className="control button"
        type="button"
        onClick={() => void saveAndLogProduct()}
        disabled={
          !addDraft.brand.trim() ||
          !addDraft.name.trim() ||
          addPreview.length === 0 ||
          addSaving
        }
      >
        {addSaving ? "Saving…" : "Save to My products"}
      </button>
    </div>
  );

  return (
    <div className="search" ref={rootRef}>
      <input
        className="control search-input"
        type="search"
        placeholder="Search a medication, supplement, food or drink"
        value={query}
        onChange={(e) => handleChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        role="combobox"
        aria-label="Product"
        aria-expanded={showDropdown}
        aria-controls={listId}
        aria-autocomplete="list"
        autoComplete="off"
      />
      {query && (
        <button
          type="button"
          className="search-clear"
          onClick={clear}
          aria-label="Clear search"
        >
          ×
        </button>
      )}
      {showDropdown && (
        <ul className="search-results" id={listId} role="listbox">
          {error ? (
            <li className="search-status">Search failed: {error}</li>
          ) : results.length === 0 ? (
            <li className="search-status">
              {searching ? (
                "Searching…"
              ) : !canAddProduct ? (
                "No products found. Try a brand or generic name."
              ) : (
                <>
                  {`No products found for "${query.trim()}". Try searching by ` +
                    "brand name (e.g. Tylenol) or generic name (e.g. acetaminophen)."}
                  {addControl}
                </>
              )}
            </li>
          ) : (
            results.map((p, i) => (
              <li
                key={p.id}
                role="option"
                aria-selected={i === active}
                className={i === active ? "search-result active" : "search-result"}
                onPointerEnter={() => setActive(i)}
                onClick={() => choose(p)}
              >
                <span className="search-brand">
                  {p.brand_name}
                  {kindLabel(p) && <span className="search-kind">{kindLabel(p)}</span>}
                  {(p.modeled ?? productModelable(p)) && (
                    <span className="search-estimate">Concentration estimate</span>
                  )}
                  {sourceLabel(p) && <span className="tag tag-source">{sourceLabel(p)}</span>}
                </span>
                <span className="search-ingredients">
                  {p.ingredients.length > 0
                    ? p.ingredients.map(ingredientLabel).join(" · ")
                    : "Ingredients unavailable"}
                </span>
              </li>
            ))
          )}
          {results.length > 0 && canAddProduct && !searching && (
            <li className="search-status">{addControl}</li>
          )}
          {total > results.length && (
            <li className="search-status muted">
              Showing {results.length} of {total} results. Keep typing to
              narrow it down.
            </li>
          )}
        </ul>
      )}
      {!showDropdown && !selected && recent.length > 0 && (
        <div className="recent">
          <div className="recent-label">Recent</div>
          <ul className="recent-list">
            {recent.map((r) => (
              <li key={r.product_id}>
                <button
                  type="button"
                  className="recent-item"
                  title={r.product_name}
                  disabled={recentLoadingId === r.product_id}
                  onClick={() => void chooseRecent(r)}
                >
                  {recentLoadingId === r.product_id
                    ? "Loading…"
                    : truncateProductName(r.product_name)}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
