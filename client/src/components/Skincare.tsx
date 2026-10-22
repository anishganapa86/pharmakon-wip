import { useEffect, useState } from "react";
import {
  checkSkincare,
  checkSkincareAlternatives,
  deleteMyProduct,
  fetchMyProducts,
  fetchSkincareCheck,
} from "../api";
import type {
  MyProductEntry,
  SkincareAlternativesGroup,
  SkincareAlternativesResponse,
  SkincareCheckResponse,
  SkincareProductRequest,
} from "../types/contract";
import { RulesNotice } from "./RulesNotice";

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

/** Clear ingredients, grouped by product, in first-seen product order. */
function groupByProduct(
  entries: { ingredient: string; product: string }[],
): [string, string[]][] {
  const order: string[] = [];
  const byProduct = new Map<string, string[]>();
  for (const { ingredient, product } of entries) {
    if (!byProduct.has(product)) {
      byProduct.set(product, []);
      order.push(product);
    }
    byProduct.get(product)!.push(ingredient);
  }
  return order.map((product) => [product, byProduct.get(product)!]);
}

/** The alternatives group for one flagged (ingredient, product) pair, if the
 * alternatives response has one -- both sides come from the same underlying
 * skincare.check_ingredients() call on the same product list, so the strings
 * line up exactly. */
function findGroup(
  resp: SkincareAlternativesResponse | null,
  ingredient: string,
  product: string,
): SkincareAlternativesGroup | undefined {
  if (!resp) return undefined;
  const ing = ingredient.trim().toLowerCase();
  const prod = product.trim().toLowerCase();
  return resp.results.find(
    (g) =>
      g.flagged_ingredient.trim().toLowerCase() === ing &&
      g.product_name.trim().toLowerCase() === prod,
  );
}

function AlternativesSection({
  ingredient,
  group,
  disclaimer,
}: {
  ingredient: string;
  group: SkincareAlternativesGroup | undefined;
  disclaimer: string | undefined;
}) {
  const candidates = group?.alternatives ?? [];
  const samePairIngredients = group?.same_product_partners ?? [];
  return (
    <details className="collapsible alternatives-section">
      <summary>Products without {ingredient}</summary>
      <div className="collapsible-body">
        {samePairIngredients.length > 0 && (
          <p className="muted small">
            Replacing this product removes both ingredients from your
            routine. Products below are only checked for the ingredients
            named on each card.
          </p>
        )}
        {candidates.length === 0 ? (
          <p className="muted small">
            No alternatives in our curated set. That doesn't mean none exist.
          </p>
        ) : (
          <>
            {candidates.map((c, i) => (
              <div key={i} className="alternative-candidate">
                <p>
                  <strong>{c.brand}</strong> {c.name}{" "}
                  <span className="muted small">({c.category})</span>
                </p>
                <p className="muted small">{c.checked_note}</p>
                {c.same_product_note && (
                  <p className="muted small">{c.same_product_note}</p>
                )}
                <details className="collapsible">
                  <summary>Ingredients</summary>
                  <p className="muted small collapsible-body">
                    {c.ingredients.join(", ")}
                  </p>
                </details>
                <p className="muted small">
                  <a href={c.source_url} target="_blank" rel="noreferrer">
                    Source
                  </a>
                  {" · "}
                  <a href={c.search_url} target="_blank" rel="noreferrer">
                    Search on Google
                  </a>
                </p>
              </div>
            ))}
            {disclaimer && <p className="muted small">{disclaimer}</p>}
          </>
        )}
      </div>
    </details>
  );
}

function SkincareResults({
  result,
  alternatives,
}: {
  result: SkincareCheckResponse;
  alternatives: SkincareAlternativesResponse | null;
}) {
  const flagged = result.incompatibilities.length > 0 || result.irritants.length > 0;

  if (result.rules_checked === 0) {
    return (
      <p className="muted">
        No rules are being applied, so these ingredients were not checked.
      </p>
    );
  }

  return (
    <>
      {!flagged && result.clear.length === 0 && (
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
          <p className="muted small">
            Matched on: '{f.matched_ingredient_a}' (rule term: '
            {f.matched_alias_a}') and '{f.matched_ingredient_b}' (rule term: '
            {f.matched_alias_b}')
          </p>
          <p className="muted small">Source: {f.source}</p>
          <AlternativesSection
            ingredient={f.ingredient_a}
            group={findGroup(alternatives, f.ingredient_a, f.product_a)}
            disclaimer={alternatives?.disclaimer}
          />
          <AlternativesSection
            ingredient={f.ingredient_b}
            group={findGroup(alternatives, f.ingredient_b, f.product_b)}
            disclaimer={alternatives?.disclaimer}
          />
        </div>
      ))}
      {result.irritants.map((f, i) => (
        <div key={`irritant-${i}`} className="skincare-flag skincare-flag-caution">
          <p>
            <strong>{f.ingredient}</strong> ({f.product})
          </p>
          <p>{f.effect}</p>
          {f.note && <p className="muted small">{f.note}</p>}
          <p className="muted small">
            Matched on: '{f.matched_ingredient}' (rule term: '{f.matched_alias}
            ')
          </p>
          <p className="muted small">Source: {f.source}</p>
          <AlternativesSection
            ingredient={f.ingredient}
            group={findGroup(alternatives, f.ingredient, f.product)}
            disclaimer={alternatives?.disclaimer}
          />
        </div>
      ))}
      {result.clear.length > 0 && (
        <details className="collapsible skincare-clear">
          <summary>
            {result.clear.length} ingredient{result.clear.length === 1 ? "" : "s"}{" "}
            had no matching rule among our {result.rules_checked} documented
            rule{result.rules_checked === 1 ? "" : "s"}. This does not mean
            none are irritating or incompatible.
          </summary>
          <div className="collapsible-body">
            {groupByProduct(result.clear).map(([product, ingredients]) => (
              <div key={product} className="skincare-clear-group">
                <div className="skincare-clear-product">{product}</div>
                <ul className="effect-tags">
                  {ingredients.map((ingredient, i) => (
                    <li key={i} className="effect-tag effect-tag-clear">
                      {ingredient}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
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
  const [autoAlternatives, setAutoAlternatives] =
    useState<SkincareAlternativesResponse | null>(null);

  const [myProducts, setMyProducts] = useState<MyProductEntry[]>([]);
  const [myProductsLoading, setMyProductsLoading] = useState(true);
  const [myProductsError, setMyProductsError] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<DraftProduct[]>([{ ...EMPTY_DRAFT }]);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<SkincareCheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [alternatives, setAlternatives] =
    useState<SkincareAlternativesResponse | null>(null);

  // Best-effort: alternatives are an enhancement on top of the check result,
  // and a failure here must never hide the check itself.
  function loadAlternatives(
    products: SkincareProductRequest[],
    setter: (r: SkincareAlternativesResponse | null) => void,
  ) {
    setter(null);
    checkSkincareAlternatives(products)
      .then(setter)
      .catch(() => setter(null));
  }

  // Mounts once per tab visit -- App.tsx only renders this component while
  // the Skincare tab is active, so switching to it always gets a fresh check.
  useEffect(() => {
    let cancelled = false;
    fetchSkincareCheck(userId)
      .then((r) => {
        if (cancelled) return;
        setAutoResult(r);
        if (r.products.length > 0) loadAlternatives(r.products, setAutoAlternatives);
      })
      .catch((e: unknown) => !cancelled && setAutoError(errorMessage(e)))
      .finally(() => !cancelled && setAutoLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  useEffect(() => {
    let cancelled = false;
    fetchMyProducts(userId)
      .then((r) => !cancelled && setMyProducts(r))
      .catch((e: unknown) => !cancelled && setMyProductsError(errorMessage(e)))
      .finally(() => !cancelled && setMyProductsLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  async function handleDeleteMyProduct(id: string) {
    try {
      await deleteMyProduct(userId, id);
      setMyProducts((prev) => prev.filter((p) => p.id !== id));
    } catch (e) {
      setMyProductsError(errorMessage(e));
    }
  }

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
      loadAlternatives(products, setAlternatives);
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
      <RulesNotice notice={(autoResult ?? result)?.rules_awaiting_verification} />

      <section className="panel">
        <h2>Products logged today</h2>
        {autoLoading ? (
          <p className="muted">Loading…</p>
        ) : autoError ? (
          <p className="error">Couldn't check: {autoError}</p>
        ) : !autoResult || autoResult.products.length === 0 ? (
          <p className="muted">
            Log a skincare product or enter ingredients manually to check for
            interactions.
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
              <SkincareResults result={autoResult} alternatives={autoAlternatives} />
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <h2>My products</h2>
        {myProductsLoading ? (
          <p className="muted">Loading…</p>
        ) : myProductsError ? (
          <p className="error">Couldn't load: {myProductsError}</p>
        ) : myProducts.length === 0 ? (
          <p className="muted">
            When a product search turns up nothing, you can add it with its
            ingredient list; it's saved here so you don't have to re-type it.
          </p>
        ) : (
          <ul className="history">
            {myProducts.map((p) => (
              <li key={p.id} className="history-item">
                <div className="history-head">
                  <strong>
                    {p.brand} {p.name}
                  </strong>
                  <span className="muted small">({p.category})</span>
                  <button
                    type="button"
                    className="button-link"
                    onClick={() => void handleDeleteMyProduct(p.id)}
                    aria-label={`Delete ${p.brand} ${p.name}`}
                  >
                    Delete
                  </button>
                </div>
                <span className="muted small">{p.ingredients.join(", ")}</span>
                <p className="muted small">
                  You entered this list. We haven't verified it. Check it
                  against your packaging.
                </p>
              </li>
            ))}
          </ul>
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
          <SkincareResults result={result} alternatives={alternatives} />
        </section>
      )}
    </>
  );
}
