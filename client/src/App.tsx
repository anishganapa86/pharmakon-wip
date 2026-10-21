import { useCallback, useEffect, useState } from "react";
import "./App.css";
import {
  fetchAlerts,
  fetchCurve,
  fetchDoses,
  fetchFoodGuidance,
  fetchInteractionCheck,
  fetchSchedule,
  fetchSkincareCheck,
  logDose,
} from "./api";
import { AlertList } from "./components/AlertList";
import {
  ConcentrationChart,
  type TimeWindow,
} from "./components/ConcentrationChart";
import { FoodGuidance } from "./components/FoodGuidance";
import { Interactions } from "./components/Interactions";
import { LogForm } from "./components/LogForm";
import { ProductName } from "./components/ProductName";
import { ProductSearch } from "./components/ProductSearch";
import { Schedule } from "./components/Schedule";
import { SideEffectProfile } from "./components/SideEffectProfile";
import { Skincare } from "./components/Skincare";
import { SymptomCheck } from "./components/SymptomCheck";
import { Timeline } from "./components/Timeline";
import {
  ingredientLabel,
  loggedIngredientLabel,
  notModeledReason,
  productModelable,
} from "./labels";
import { formatDateClock, HOUR_MS } from "./time";
import type {
  Alert,
  LoggedDoseEvent,
  ProductSearchResult,
  RecentProduct,
  ScheduledMedication,
  SubstanceCurve,
} from "./types/contract";

type View =
  | "concentration"
  | "timeline"
  | "skincare"
  | "interactions"
  | "food";

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
  doses: LoggedDoseEvent[]; // oldest first; for the dose history/timeline
  schedule: ScheduledMedication[];
}

/**
 * Curves for every substance with a modeled dose in the last 24 hours or in a
 * scheduled medication, from one hour before the earliest recent dose (six
 * hours back if there is none) to six hours past now, or 24 when anything is
 * scheduled, so the next scheduled dose shows up in the projection.
 */
async function fetchDashboard(): Promise<Dashboard> {
  const nowMs = Date.now();
  const [doses, alerts, schedule] = await Promise.all([
    fetchDoses(USER_ID),
    fetchAlerts(USER_ID),
    fetchSchedule(USER_ID),
  ]);
  const recentLogged = recentProducts(doses);

  const recent = doses.filter(
    (d: LoggedDoseEvent) => new Date(d.taken_at).getTime() >= nowMs - 24 * HOUR_MS,
  );
  // Unmodeled ingredients have no curve; they're named in the alerts instead.
  const substances = [
    ...new Set([
      ...recent.flatMap((d) =>
        d.ingredients
          .filter((i) => i.substance_id !== null && i.unmodeled_reason === null)
          .map((i) => i.substance_id as string),
      ),
      ...schedule.flatMap((m) =>
        m.substances
          .filter((s) => s.substance_id !== null && s.unmodeled_reason === null)
          .map((s) => s.substance_id as string),
      ),
    ]),
  ];
  if (recent.length === 0 && substances.length === 0) {
    return {
      doseCount: doses.length,
      window: null,
      curves: [],
      alerts,
      recent: recentLogged,
      doses,
      schedule,
    };
  }

  const earliest =
    recent.length > 0
      ? Math.min(...recent.map((d) => new Date(d.taken_at).getTime())) - HOUR_MS
      : nowMs - 6 * HOUR_MS;
  const window = {
    fromMs: earliest,
    toMs: nowMs + (schedule.length > 0 ? 24 : 6) * HOUR_MS,
    nowMs,
  };
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
  return {
    doseCount: doses.length,
    window,
    curves,
    alerts,
    recent: recentLogged,
    doses,
    schedule,
  };
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

  const [view, setView] = useState<View>("concentration");
  const [timelineKey, setTimelineKey] = useState(0); // bump to force a refetch
  const [sideEffectKey, setSideEffectKey] = useState(0); // bump to force a refetch
  const [skincareNotice, setSkincareNotice] = useState(false);
  const [interactionsNotice, setInteractionsNotice] = useState(false);
  // Lowercased names/product names with a quoted take_with_food finding --
  // drives the small "With food" chip on each Schedule card.
  const [withFoodNames, setWithFoodNames] = useState<Set<string>>(new Set());

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
    void refreshFoodGuidanceChips();
  }, [load]);

  function reload() {
    setLoading(true);
    void load();
  }

  // Best-effort: a failed skincare or interaction check must never block a
  // dose that was already recorded.
  async function refreshSkincareNotice() {
    try {
      const check = await fetchSkincareCheck(USER_ID);
      setSkincareNotice(
        check.incompatibilities.length > 0 || check.irritants.length > 0,
      );
    } catch {
      // Leave the existing notice state as-is.
    }
  }

  async function refreshInteractionsNotice() {
    try {
      const check = await fetchInteractionCheck(USER_ID);
      setInteractionsNotice(check.interactions.length > 0);
    } catch {
      // Leave the existing notice state as-is.
    }
  }

  async function refreshFoodGuidanceChips() {
    try {
      const check = await fetchFoodGuidance(USER_ID);
      setWithFoodNames(
        new Set(
          check.guidance
            .filter((f) => f.class === "take_with_food")
            .map((f) => f.name.trim().toLowerCase()),
        ),
      );
    } catch {
      // Leave the existing chip state as-is.
    }
  }

  // After a schedule change or a dose taken from it: same refresh as manual
  // logging, since "Take now" records a real dose.
  async function handleScheduleChanged() {
    setTimelineKey((k) => k + 1);
    setSideEffectKey((k) => k + 1);
    setLoading(true);
    await load();
    await refreshSkincareNotice();
    await refreshInteractionsNotice();
    await refreshFoodGuidanceChips();
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
      setTimelineKey((k) => k + 1);
      setSideEffectKey((k) => k + 1);
      setLoading(true);
      await load();
      await refreshSkincareNotice();
      await refreshInteractionsNotice();
      await refreshFoodGuidanceChips();
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

      <Schedule
        userId={USER_ID}
        medications={dashboard?.schedule ?? null}
        recent={dashboard?.recent ?? []}
        onChanged={() => void handleScheduleChanged()}
        interactionsNotice={interactionsNotice}
        onViewInteractions={() => setView("interactions")}
        withFoodNames={withFoodNames}
      />

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
        {product && !productModelable(product) && (
          <p className="warning">
            {notModeledReason(product.route)}. It will appear in your log and
            timeline, but no concentration curve is drawn.
          </p>
        )}
        {logged && <p className="muted">{logged}</p>}
        {logError && <p className="error">Couldn't log dose: {logError}</p>}
      </section>

      <nav className="view-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={view === "concentration"}
          className={view === "concentration" ? "view-tab active" : "view-tab"}
          onClick={() => setView("concentration")}
        >
          Concentration
        </button>
        <button
          role="tab"
          aria-selected={view === "timeline"}
          className={view === "timeline" ? "view-tab active" : "view-tab"}
          onClick={() => setView("timeline")}
        >
          Timeline
        </button>
        <button
          role="tab"
          aria-selected={view === "skincare"}
          className={view === "skincare" ? "view-tab active" : "view-tab"}
          onClick={() => setView("skincare")}
        >
          Skincare
        </button>
        <button
          role="tab"
          aria-selected={view === "interactions"}
          className={view === "interactions" ? "view-tab active" : "view-tab"}
          onClick={() => setView("interactions")}
        >
          Interactions
        </button>
        <button
          role="tab"
          aria-selected={view === "food"}
          className={view === "food" ? "view-tab active" : "view-tab"}
          onClick={() => setView("food")}
        >
          Food & drink
        </button>
      </nav>

      {view === "skincare" && <Skincare userId={USER_ID} />}

      {view === "interactions" && <Interactions userId={USER_ID} />}

      {view === "food" && <FoodGuidance userId={USER_ID} />}

      {view === "timeline" && (
        <>
          <section className="panel">
            <p className="warning timeline-disclaimer">
              This is your personal log. It does not diagnose conditions or
              identify causes. Patterns you notice are worth discussing with
              your doctor.
            </p>
            <h2>Add to your timeline</h2>
            <LogForm
              userId={USER_ID}
              onLogged={() => setTimelineKey((k) => k + 1)}
            />
          </section>

          <section className="panel">
            <h2>Timeline</h2>
            <Timeline
              key={timelineKey}
              userId={USER_ID}
              onDeleted={() => setTimelineKey((k) => k + 1)}
            />
          </section>
        </>
      )}

      {view === "concentration" && (
        <>
          {skincareNotice && (
            <p className="warning">
              We found potential interactions in your skincare routine.
              Check the{" "}
              <button
                className="button-link"
                onClick={() => setView("skincare")}
              >
                Skincare tab
              </button>
              .
            </p>
          )}
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
            ) : dashboard.curves.length === 0 ? (
              <p className="muted">
                {dashboard.doseCount === 0
                  ? "No doses logged yet."
                  : dashboard.window === null
                    ? "No doses in the last 24 hours."
                    : "None of the recent doses have a modeled ingredient to chart."}
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
            <SideEffectProfile key={sideEffectKey} userId={USER_ID} />
          </section>

          <section className="panel">
            <h2>Dose history</h2>
            {dashboard === null || dashboard.doses.length === 0 ? (
              <p className="muted">No doses logged yet.</p>
            ) : (
              <ul className="history">
                {[...dashboard.doses].reverse().map((d) => {
                  const modeled = d.ingredients.some(
                    (i) => i.substance_id !== null && i.unmodeled_reason === null,
                  );
                  return (
                    <li key={d.dose_event_id} className="history-item">
                      <div className="history-head">
                        <strong>
                          <ProductName name={d.product_name} />
                        </strong>
                        <span className="muted">
                          {formatDateClock(new Date(d.taken_at).getTime())}
                        </span>
                        <span className="muted small">
                          {d.ingredients.map(loggedIngredientLabel).join(" · ")}
                        </span>
                      </div>
                      {modeled ? (
                        <span className="tag tag-modelable">Modeled</span>
                      ) : (
                        // Route isn't tracked on a logged dose, only at search
                        // time, so history uses the generic honest phrasing.
                        <span className="tag tag-logged-only">
                          {notModeledReason(null)}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
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
        </>
      )}

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
