import { useCallback, useEffect, useRef, useState } from "react";
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
  type AuthUser,
} from "./api";
import { AlertList } from "./components/AlertList";
import {
  ConcentrationChart,
  type TimeWindow,
} from "./components/ConcentrationChart";
import { FoodGuidance } from "./components/FoodGuidance";
import { Hero } from "./components/Hero";
import { Interactions } from "./components/Interactions";
import { LogForm } from "./components/LogForm";
import { ProductName } from "./components/ProductName";
import { ProductSearch } from "./components/ProductSearch";
import { Schedule, type FoodChip } from "./components/Schedule";
import { SideEffectProfile } from "./components/SideEffectProfile";
import { Skincare } from "./components/Skincare";
import { SymptomCheck } from "./components/SymptomCheck";
import { Timeline } from "./components/Timeline";
import { Icon, type IconName } from "./components/Icon";
import { TopicalIngredients } from "./components/TopicalIngredients";
import {
  ingredientLabel,
  loggedIngredientLabel,
  notModeledReason,
  productModelable,
} from "./labels";
import {
  LOG_HINT,
  TAB_HINTS,
  TAB_LABELS,
  scrollsToSchedule,
  showsHints,
  startingView,
  type Survey,
  type View,
} from "./survey";
import { formatDateClock, HOUR_MS } from "./time";
import type {
  Alert,
  LoggedDoseEvent,
  ProductSearchResult,
  RecentProduct,
  ScheduledMedication,
  SubstanceCurve,
} from "./types/contract";

const MAX_RECENT_PRODUCTS = 5;
const TAB_ICONS: Record<string, IconName> = {
  concentration: "home",
  skincare: "sparkle",
  interactions: "alert",
};

const TAB_NAMES: Record<string, string> = {
  concentration: "Today",
  interactions: "Interactions",
  skincare: "Skincare",
};

function sameLocalDay(a: Date, b: Date): boolean {
  return a.toDateString() === b.toDateString();
}

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
async function fetchDashboard(userId: string): Promise<Dashboard> {
  const nowMs = Date.now();
  const [doses, alerts, schedule] = await Promise.all([
    fetchDoses(userId),
    fetchAlerts(userId),
    fetchSchedule(userId),
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
      fetchCurve(userId, {
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

export interface TabRequest {
  view: View;
  nonce: number;
}

interface AppProps {
  onViewChange: (view: View) => void;
  onOpenScan: () => void;
  tabRequest: TabRequest | null;
  user: AuthUser;
  onLogOut: () => void;
  survey: Survey;
  onOpenSettings: () => void;
}

function App({
  user,
  onLogOut,
  survey,
  onOpenSettings,
  onOpenScan,
  onViewChange,
  tabRequest,
}: AppProps) {
  const tailoring = survey.status === "completed" ? survey : null;
  const hints = showsHints(tailoring);
  const userId = user.id;
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [product, setProduct] = useState<ProductSearchResult | null>(null);
  const [searchKey, setSearchKey] = useState(0); // bump to reset the search box
  const [quantity, setQuantity] = useState("1");
  const [logging, setLogging] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const [logged, setLogged] = useState<string | null>(null);

  const [view, setView] = useState<View>(() => startingView(tailoring));
  useEffect(() => {
    onViewChange(view);
  }, [view, onViewChange]);
  const [handledNonce, setHandledNonce] = useState(0);
  if (tabRequest && tabRequest.nonce !== handledNonce) {
    setHandledNonce(tabRequest.nonce);
    setView(tabRequest.view);
  }
  const [moreOpen, setMoreOpen] = useState(false);
  const scheduleRef = useRef<HTMLDivElement>(null);
  const jumpToSchedule = scrollsToSchedule(tailoring);
  useEffect(() => {
    if (jumpToSchedule) scheduleRef.current?.scrollIntoView({ block: "start" });
  }, [jumpToSchedule]);
  const [timelineKey, setTimelineKey] = useState(0); // bump to force a refetch
  const [sideEffectKey, setSideEffectKey] = useState(0); // bump to force a refetch
  const [skincareNotice, setSkincareNotice] = useState(false);
  const [interactionsNotice, setInteractionsNotice] = useState(false);
  const [withFoodNames, setWithFoodNames] = useState<Map<string, FoodChip>>(
    new Map(),
  );

  // State is set only in promise callbacks, so this is safe to run from an
  // effect. Callers that re-load set `loading` themselves.
  const load = useCallback(
    () =>
      fetchDashboard(userId)
        .then((d) => {
          setDashboard(d);
          setError(null);
        })
        .catch((e: unknown) => setError(errorMessage(e)))
        .finally(() => setLoading(false)),
    [userId],
  );

  const refreshFoodGuidanceChips = useCallback(
    () =>
      fetchFoodGuidance(userId)
        .then((check) => {
          const chips = new Map<string, FoodChip>();
          for (const f of check.guidance) {
            if (f.class !== "take_with_food" && f.class !== "take_with_food_if_upset")
              continue;
            const name = f.name.trim().toLowerCase();
            if (f.class === "take_with_food" || !chips.has(name))
              chips.set(name, f.class);
          }
          setWithFoodNames(chips);
        })
        .catch(() => undefined),
    [userId],
  );

  useEffect(() => {
    void load();
    void refreshFoodGuidanceChips();
  }, [load, refreshFoodGuidanceChips]);

  function reload() {
    setLoading(true);
    void load();
  }

  // Best-effort: a failed skincare or interaction check must never block a
  // dose that was already recorded.
  async function refreshSkincareNotice() {
    try {
      const check = await fetchSkincareCheck(userId);
      setSkincareNotice(
        check.incompatibilities.length > 0 || check.irritants.length > 0,
      );
    } catch {
      // Leave the existing notice state as-is.
    }
  }

  async function refreshInteractionsNotice() {
    try {
      const check = await fetchInteractionCheck(userId);
      setInteractionsNotice(check.interactions.length > 0);
    } catch {
      // Leave the existing notice state as-is.
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

  async function logProduct(p: ProductSearchResult, qtyToLog: number) {
    setLogging(true);
    setLogError(null);
    setLogged(null);
    try {
      await logDose(userId, {
        product_id: p.id,
        taken_at: new Date().toISOString(),
        quantity: qtyToLog,
      });
      setLogged(`Logged ${qtyToLog} × ${p.brand_name}.`);
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

  async function handleLogDose(e: React.FormEvent) {
    e.preventDefault();
    if (!product || !qtyValid) return;
    await logProduct(product, qty);
  }

  return (
    <main className={`page view-${view}`}>
      <header className="page-header">
        <div className="page-header-row">
          <h1>Pharmakon</h1>
          <div className="account">
            <span className="account-email" title={user.email}>
              {user.email}
            </span>
            <button
              type="button"
              className="control secondary"
              onClick={onOpenSettings}
            >
              Settings
            </button>
            <button
              type="button"
              className="control secondary logout"
              onClick={onLogOut}
            >
              Log out
            </button>
          </div>
        </div>
      </header>

      <div className="screen-head">
        <div>
          {view === "concentration" && (
            <p className="screen-date">
              {new Date().toLocaleDateString(undefined, {
                weekday: "long",
                month: "long",
                day: "numeric",
              })}
            </p>
          )}
          <div className="screen-title" aria-hidden="true">
            {view === "concentration" ? "Today" : TAB_LABELS[view]}
          </div>
        </div>
        {view === "concentration" && (
          <button
            type="button"
            className="avatar"
            aria-label="Your account"
            onClick={onOpenSettings}
          >
            <Icon name="user" size={20} />
          </button>
        )}
      </div>

      {view === "concentration" && dashboard && (
        <Hero
          alerts={dashboard.alerts}
          doseCount={dashboard.doseCount}
          onDetails={() =>
            document
              .querySelector(".today-alerts")
              ?.scrollIntoView({ block: "start", behavior: "smooth" })
          }
        />
      )}

      <div ref={scheduleRef} className="schedule-wrap">
      <Schedule
        userId={userId}
        medications={dashboard?.schedule ?? null}
        recent={dashboard?.recent ?? []}
        onChanged={() => void handleScheduleChanged()}
        interactionsNotice={interactionsNotice}
        onViewInteractions={() => setView("interactions")}
        withFoodNames={withFoodNames}
      />
      </div>

      <section className="panel log-panel">
        <div className="panel-title-row">
          <h2>Log intake</h2>
          <button type="button" className="control secondary" onClick={onOpenScan}>
            Scan
          </button>
        </div>
        {hints && <p className="hint">{LOG_HINT}</p>}
        <form className="log-row" onSubmit={handleLogDose}>
          <ProductSearch
            key={searchKey}
            userId={userId}
            selected={product}
            onSelect={(p) => {
              setProduct(p);
              setLogged(null);
            }}
            recent={dashboard?.recent ?? []}
            onSavedProductLogged={(p) => void logProduct(p, 1)}
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

      <nav className="bottom-bar" aria-label="Main">
        {(["concentration", "interactions"] as View[]).map((tab) => (
          <button
            key={tab}
            type="button"
            className={view === tab ? "bottom-item active" : "bottom-item"}
            aria-current={view === tab ? "page" : undefined}
            onClick={() => {
              setView(tab);
              setMoreOpen(false);
            }}
          >
            <Icon name={TAB_ICONS[tab]} size={22} />
            <span>{TAB_NAMES[tab]}</span>
          </button>
        ))}
        <button
          type="button"
          className="bottom-item"
          onClick={() => {
            setMoreOpen(false);
            onOpenScan();
          }}
        >
          <Icon name="scan" size={22} />
          <span>Scan</span>
        </button>
        <button
          type="button"
          className={view === "skincare" ? "bottom-item active" : "bottom-item"}
          aria-current={view === "skincare" ? "page" : undefined}
          onClick={() => {
            setView("skincare");
            setMoreOpen(false);
          }}
        >
          <Icon name="sparkle" size={22} />
          <span>Skincare</span>
        </button>
        <button
          type="button"
          className={
            moreOpen || view === "timeline" || view === "food"
              ? "bottom-item active"
              : "bottom-item"
          }
          aria-expanded={moreOpen}
          onClick={() => setMoreOpen(!moreOpen)}
        >
          <Icon name="menu" size={22} />
          <span>More</span>
        </button>
      </nav>

      {moreOpen && (
        <div className="more-menu">
          <button
            type="button"
            onClick={() => {
              setView("timeline");
              setMoreOpen(false);
            }}
          >
            <Icon name="clock" size={20} />
            <span>{TAB_LABELS.timeline}</span>
            <Icon name="chevronRight" size={16} />
          </button>
          <button
            type="button"
            onClick={() => {
              setView("food");
              setMoreOpen(false);
            }}
          >
            <Icon name="coffee" size={20} />
            <span>{TAB_LABELS.food}</span>
            <Icon name="chevronRight" size={16} />
          </button>
          <button
            type="button"
            onClick={() => {
              setMoreOpen(false);
              onOpenSettings();
            }}
          >
            <Icon name="menu" size={20} />
            <span>Settings</span>
            <Icon name="chevronRight" size={16} />
          </button>
          <button type="button" className="more-logout" onClick={onLogOut}>
            Log out
          </button>
        </div>
      )}

      {hints && <p className="hint">{TAB_HINTS[view]}</p>}

      {view === "skincare" && <Skincare userId={userId} />}

      {view === "interactions" && <Interactions userId={userId} />}

      {view === "food" && <FoodGuidance userId={userId} />}

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
              userId={userId}
              onLogged={() => setTimelineKey((k) => k + 1)}
            />
          </section>

          <section className="panel">
            <h2>Timeline</h2>
            <Timeline
              key={timelineKey}
              userId={userId}
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
          <section className="panel today-chart">
            <div className="section-row">
              <h2>Concentration</h2>
              <span className="section-note">Estimated</span>
            </div>
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
            <SideEffectProfile key={sideEffectKey} userId={userId} />
          </section>

          <section className="panel today-logged">
            <div className="section-row">
              <h2>Logged today</h2>
              <button
                type="button"
                className="button-link section-link"
                onClick={() =>
                  document
                    .querySelector(".log-panel")
                    ?.scrollIntoView({ block: "start", behavior: "smooth" })
                }
              >
                Log intake
              </button>
            </div>
            {dashboard === null ||
            !dashboard.doses.some((d) => sameLocalDay(new Date(d.taken_at), new Date())) ? (
              <p className="muted">Nothing logged today.</p>
            ) : (
              <ul className="rows">
                {[...dashboard.doses]
                  .reverse()
                  .filter((d) => sameLocalDay(new Date(d.taken_at), new Date()))
                  .map((d) => (
                    <li key={d.dose_event_id} className="row">
                      <div className="row-main">
                        <strong>
                          <ProductName name={d.product_name} />
                        </strong>
                        <span className="muted small">Quantity {d.quantity}</span>
                      </div>
                      <span className="row-time">
                        {new Date(d.taken_at).toLocaleTimeString([], {
                          hour: "numeric",
                          minute: "2-digit",
                        })}
                      </span>
                    </li>
                  ))}
              </ul>
            )}
          </section>

          <section className="panel today-history">
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
                        {!d.topical && (
                          <span className="muted small">
                            {d.ingredients.map(loggedIngredientLabel).join(" · ")}
                          </span>
                        )}
                      </div>
                      {d.topical && <TopicalIngredients ingredients={d.ingredients} />}
                      {modeled ? (
                        <span className="search-estimate">Concentration estimate</span>
                      ) : (
                        <span className="tag tag-logged-only">
                          {d.topical
                            ? notModeledReason("TOPICAL")
                            : notModeledReason(null)}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section className="panel today-feeling">
            <h2>How are you feeling?</h2>
            <SymptomCheck />
          </section>

          <section className="panel today-alerts">
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
