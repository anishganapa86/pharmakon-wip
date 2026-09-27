import { useEffect, useState } from "react";
import { checkSkincare, fetchSkincareCheck } from "../api";
import type { SkincareCheckResponse } from "../types/contract";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

interface DraftProduct {
  name: string;
  ingredientsText: string; // comma-separated, as typed
}

function parseIngredients(text: string): string[] {
  return text
    .split(",")
    .map((i) => i.trim())
    .filter((i) => i.length > 0);
}

const EMPTY_DRAFT: DraftProduct = { name: "", ingredientsText: "" };

function SkincareResults({ result }: { result: SkincareCheckResponse }) {
  return (
    <>
      {result.incompatibilities.length === 0 && result.irritants.length === 0 && (
        <p className="muted">No documented interactions found.</p>
      )}
      {result.incompatibilities.map((f, i) => (
        <div key={`incompat-${i}`} className="skincare-flag skincare-flag-caution">
          <p>
            <strong>{f.ingredient_a}</strong> ({f.product_a}) +{" "}
            <strong>{f.ingredient_b}</strong> ({f.product_b})
          </p>
          <p>{f.effect}</p>
          <p className="muted small">{f.recommendation}</p>
          <p className="muted small">Source: {f.source}</p>
        </div>
      ))}
      {result.irritants.map((f, i) => (
        <div key={`irritant-${i}`} className="skincare-flag skincare-flag-caution">
          <p>
            <strong>{f.ingredient}</strong> ({f.product})
          </p>
          <p>{f.effect}</p>
          {f.note && <p className="muted small">{f.note}</p>}
          <p className="muted small">Source: {f.source}</p>
        </div>
      ))}
      {result.clear.length > 0 && (
        <div className="skincare-clear">
          <div className="skincare-clear-label">Clear</div>
          <ul className="effect-tags">
            {result.clear.map((c, i) => (
              <li key={i} className="effect-tag effect-tag-clear">
                {c.ingredient} ({c.product})
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

interface Props {
  userId: string;
}

export function Skincare({ userId }: Props) {
  const [autoResult, setAutoResult] = useState<SkincareCheckResponse | null>(null);
  const [autoLoading, setAutoLoading] = useState(true);
  const [autoError, setAutoError] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<DraftProduct[]>([{ ...EMPTY_DRAFT }]);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<SkincareCheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Mounts once per tab visit -- App.tsx only renders this component while
  // the Skincare tab is active, so switching to it always gets a fresh check.
  useEffect(() => {
    let cancelled = false;
    fetchSkincareCheck(userId)
      .then((r) => !cancelled && setAutoResult(r))
      .catch((e: unknown) => !cancelled && setAutoError(errorMessage(e)))
      .finally(() => !cancelled && setAutoLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function updateDraft(i: number, patch: Partial<DraftProduct>) {
    setDrafts((prev) => prev.map((d, idx) => (idx === i ? { ...d, ...patch } : d)));
  }

  function addDraft() {
    setDrafts((prev) => [...prev, { ...EMPTY_DRAFT }]);
  }

  function removeDraft(i: number) {
    setDrafts((prev) => prev.filter((_, idx) => idx !== i));
  }

  const products = drafts
    .map((d) => ({ name: d.name.trim(), ingredients: parseIngredients(d.ingredientsText) }))
    .filter((p) => p.name && p.ingredients.length > 0);

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    if (products.length === 0) return;
    setChecking(true);
    setError(null);
    try {
      setResult(await checkSkincare(products));
    } catch (err) {
      setResult(null);
      setError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <p className="warning timeline-disclaimer">
        These are documented ingredient interactions from dermatological
        literature. They are not diagnoses. Discuss changes to your skincare
        routine with a dermatologist.
      </p>

      <section className="panel">
        <h2>Products logged today</h2>
        {autoLoading ? (
          <p className="muted">Loading…</p>
        ) : autoError ? (
          <p className="error">Couldn't check: {autoError}</p>
        ) : !autoResult || autoResult.products.length === 0 ? (
          <p className="muted">
            No topical products logged in the last 24 hours.
          </p>
        ) : (
          <>
            <ul className="history">
              {autoResult.products.map((p, i) => (
                <li key={i} className="history-item">
                  <div className="history-head">
                    <strong>{p.name}</strong>
                    <span className="muted small">
                      {p.ingredients.join(", ")}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <div className="skincare-auto-results">
              <SkincareResults result={autoResult} />
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <h2>Check your products</h2>
        <form className="skincare-form" onSubmit={handleCheck}>
          {drafts.map((d, i) => (
            <div className="skincare-draft" key={i}>
              <input
                className="control"
                type="text"
                placeholder="Product name"
                value={d.name}
                onChange={(e) => updateDraft(i, { name: e.target.value })}
                aria-label="Product name"
              />
              <input
                className="control grow"
                type="text"
                placeholder="Ingredients, comma-separated (e.g. Retinol, Glycerin, Fragrance)"
                value={d.ingredientsText}
                onChange={(e) =>
                  updateDraft(i, { ingredientsText: e.target.value })
                }
                aria-label="Ingredients"
              />
              {drafts.length > 1 && (
                <button
                  type="button"
                  className="button-link"
                  onClick={() => removeDraft(i)}
                  aria-label="Remove product"
                >
                  ×
                </button>
              )}
            </div>
          ))}
          <div className="log-row">
            <button type="button" className="button-link" onClick={addDraft}>
              + Add another product
            </button>
            <button
              className="control button"
              type="submit"
              disabled={products.length === 0 || checking}
            >
              {checking ? "Checking…" : "Check"}
            </button>
          </div>
        </form>
        {error && <p className="error">Couldn't check: {error}</p>}
      </section>

      {result && (
        <section className="panel">
          <h2>Results</h2>
          <SkincareResults result={result} />
        </section>
      )}
    </>
  );
}
