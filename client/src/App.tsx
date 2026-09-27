import { useCallback, useEffect, useState } from "react";
import "./App.css";
import { fetchAlerts, fetchCurve, fetchDoses, logDose } from "./api";
import { AlertList } from "./components/AlertList";
import {
  ConcentrationChart,
  type TimeWindow,
} from "./components/ConcentrationChart";
import { ProductSearch } from "./components/ProductSearch";
import { SymptomCheck } from "./components/SymptomCheck";
import { ingredientLabel } from "./labels";
import { HOUR_MS } from "./time";
import type {
  Alert,
  LoggedDoseEvent,
  ProductSearchResult,
  RecentProduct,
  SubstanceCurve,
} from "./types/contract";

const USER_ID = "default";
const MAX_RECENT_PRODUCTS = 5;

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Last 5 distinct products logged, most recently taken first. */
function recentProducts(doses: LoggedDoseEvent[]): RecentProduct[] {
  const seen = new Set<string>();
  const out: RecentProduct[] = [];
  const byRecency = [...doses].sort(
    (a, b) => new Date(b.taken_at).getTime() - new Date(a.taken_at).getTime(),
  );
  for (const d of byRecency) {
    if (seen.has(d.product_id)) continue;
    seen.add(d.product_id);
    out.push({ product_id: d.product_id, product_name: d.product_name });
    if (out.length === MAX_RECENT_PRODUCTS) break;
  }
  return out;
}

interface Dashboard {
  doseCount: number;
  window: TimeWindow | null; // null when nothing was taken in the last 24h
  curves: SubstanceCurve[];
  alerts: Alert[];
  recent: RecentProduct[];
}

/**
 * Curves for every substance with a modeled dose in the last 24 hours, from
 * one hour before the earliest of those doses to six hours past now.
 */
async function fetchDashboard(): Promise<Dashboard> {
  const nowMs = Date.now();
  const [doses, alerts] = await Promise.all([
    fetchDoses(USER_ID),
    fetchAlerts(USER_ID),
  ]);
  const recentLogged = recentProducts(doses);

  const recent = doses.filter(
    (d: LoggedDoseEvent) => new Date(d.taken_at).getTime() >= nowMs - 24 * HOUR_MS,
  );
  if (recent.length === 0) {
    return {
      doseCount: doses.length,
      window: null,
      curves: [],
      alerts,
      recent: recentLogged,
    };
  }

  const earliest = Math.min(...recent.map((d) => new Date(d.taken_at).getTime()));
  const window = {
    fromMs: earliest - HOUR_MS,
    toMs: nowMs + 6 * HOUR_MS,
    nowMs,
  };
  // Unmodeled ingredients have no curve; they're named in the alerts instead.
  const substances = [
    ...new Set(
      recent.flatMap((d) =>
        d.ingredients
          .filter((i) => i.substance_id !== null && i.unmodeled_reason === null)
          .map((i) => i.substance_id as string),
      ),
    ),
  ];
  const curves = await Promise.all(
    substances.map((substance_id) =>
      fetchCurve(USER_ID, {
        substance_id,
        from: new Date(window.fromMs).toISOString(),
        to: new Date(window.toMs).toISOString(),
        resolution: "15m",
      }),
    ),
  );
  return { doseCount: doses.length, window, curves, alerts, recent: recentLogged };
}

function App() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [product, setProduct] = useState<ProductSearchResult | null>(null);
  const [searchKey, setSearchKey] = useState(0); // bump to reset the search box
  const [quantity, setQuantity] = useState("1");
  const [logging, setLogging] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const [logged, setLogged] = useState<string | null>(null);

  // State is set only in promise callbacks, so this is safe to run from an
  // effect. Callers that re-load set `loading` themselves.
  const load = useCallback(
    () =>
      fetchDashboard()
        .then((d) => {
          setDashboard(d);
          setError(null);
        })
        .catch((e: unknown) => setError(errorMessage(e)))
        .finally(() => setLoading(false)),
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  function reload() {
    setLoading(true);
    void load();
  }

  const qty = Number(quantity);
  const qtyValid = quantity.trim() !== "" && Number.isFinite(qty) && qty > 0;

  async function handleLogDose(e: React.FormEvent) {
    e.preventDefault();
    if (!product || !qtyValid) return;
    setLogging(true);
    setLogError(null);
    setLogged(null);
    try {
      await logDose(USER_ID, {
        product_id: product.id,
        taken_at: new Date().toISOString(),
        quantity: qty,
      });
      setLogged(`Logged ${qty} × ${product.brand_name}.`);
      setProduct(null);
      setQuantity("1");
      setSearchKey((k) => k + 1);
      setLoading(true);
      await load();
    } catch (err) {
      setLogError(errorMessage(err));
    } finally {
      setLogging(false);
    }
  }

  return (
    <main className="page">
      <header className="page-header">
        <h1>Pharmakon</h1>
      </header>

      <section className="panel">
        <h2>Log a dose</h2>
        <form className="log-row" onSubmit={handleLogDose}>
          <ProductSearch
            key={searchKey}
            selected={product}
            onSelect={(p) => {
              setProduct(p);
              setLogged(null);
            }}
            recent={dashboard?.recent ?? []}
          />
          <input
            className="control qty-input"
            type="number"
            min={0.5}
            step={0.5}
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            aria-label="Quantity"
          />
          <button
            className="control button"
            type="submit"
            disabled={!product || !qtyValid || logging}
          >
            {logging ? "Logging…" : "Log"}
          </button>
        </form>
        {product && (
          <p className="selected-product">
            <strong>{product.brand_name}</strong>:{" "}
            {product.ingredients.map(ingredientLabel).join(" · ")}
          </p>
        )}
        {product &&
          product.has_unmodeled_ingredients &&
          product.ingredients.every((i) => i.substance_id === null) && (
            <p className="warning">
              This product's ingredients are not in our database. It will
              appear in your log but concentration won't be modeled.
            </p>
          )}
        {logged && <p className="muted">{logged}</p>}
        {logError && <p className="error">Couldn't log dose: {logError}</p>}
      </section>

      <section className="panel">
        <h2>Concentration</h2>
        {error ? (
          <p className="error">
            Couldn't reach the API: {error}{" "}
            <button className="button-link" onClick={reload}>
              Retry
            </button>
          </p>
        ) : dashboard === null ? (
          <p className="muted">{loading ? "Loading…" : null}</p>
        ) : dashboard.doseCount === 0 ? (
          <p className="muted">No doses logged yet.</p>
        ) : dashboard.window === null ? (
          <p className="muted">No doses in the last 24 hours.</p>
        ) : dashboard.curves.length === 0 ? (
          <p className="muted">
            None of the recent doses have a modeled ingredient to chart.
          </p>
        ) : (
          <div className="charts">
            {dashboard.curves.map((curve) => (
              <ConcentrationChart
                key={curve.substance.id}
                curve={curve}
                window={dashboard.window as TimeWindow}
              />
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>How are you feeling?</h2>
        <SymptomCheck />
      </section>

      <section className="panel">
        <h2>Alerts</h2>
        {dashboard && !error && <AlertList alerts={dashboard.alerts} />}
      </section>

      <footer className="page-footer">
        <small>
          Not a medical device. Estimates from a one-compartment,
          population-average model, for educational and demonstrative use
          only.
        </small>
      </footer>
    </main>
  );
}

export default App;
