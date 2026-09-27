import { useEffect, useId, useRef, useState } from "react";
import { searchProducts } from "../api";
import { ingredientLabel, productModelable, sourceLabel } from "../labels";
import type { ProductSearchResult, RecentProduct } from "../types/contract";

const DEBOUNCE_MS = 200;

interface Props {
  selected: ProductSearchResult | null;
  onSelect: (product: ProductSearchResult | null) => void;
  recent: RecentProduct[];
}

export function ProductSearch({ selected, onSelect, recent }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ProductSearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [recentLoadingId, setRecentLoadingId] = useState<string | null>(null);
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
          setResults(r);
          setActive(0);
          setError(null);
        })
        .catch((e: unknown) => {
          if (controller.signal.aborted) return;
          setResults([]);
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
      const found = await searchProducts(r.product_name);
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
    setError(null);
    setOpen(false);
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

  return (
    <div className="search" ref={rootRef}>
      <input
        className="control search-input"
        type="search"
        placeholder="Search a product, e.g. NyQuil"
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
              {searching
                ? "Searching…"
                : `No products found for "${query.trim()}". Try searching by ` +
                  "brand name (e.g. Tylenol) or generic name (e.g. acetaminophen)."}
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
                  <span
                    className={
                      productModelable(p)
                        ? "tag tag-modelable"
                        : "tag tag-logged-only"
                    }
                  >
                    {productModelable(p) ? "Modeled" : "Logged only"}
                  </span>
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
                  disabled={recentLoadingId === r.product_id}
                  onClick={() => void chooseRecent(r)}
                >
                  {recentLoadingId === r.product_id
                    ? "Loading…"
                    : r.product_name}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
