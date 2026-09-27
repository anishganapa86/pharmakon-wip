import { useCallback, useEffect, useState } from "react";
import { fetchAlerts, fetchCurve, logDose } from "./api";
import { ConcentrationChart } from "./components/ConcentrationChart";
import type { Alert, SubstanceCurve } from "./types/contract";

const USER_ID = "default";
const SUBSTANCES = ["caffeine", "acetaminophen"];

// Mirrors the synthetic seed catalog in app/api/products.py.
const PRODUCTS = [
  { id: "prod_nodoz_200", name: "NoDoz" },
  { id: "prod_tylenol_es_500", name: "Tylenol Extra Strength" },
  { id: "prod_dayquil_liquicap", name: "DayQuil" },
  { id: "prod_advil_200", name: "Advil" },
];

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function AlertItem({ alert }: { alert: Alert }) {
  return (
    <li style={{ marginBottom: "1rem" }}>
      <strong>{alert.severity.toUpperCase()}</strong>
      {alert.substance && ` — ${alert.substance.display_name}`}
      <p>{alert.message}</p>
      {alert.notes.length > 0 && (
        <ul>
          {alert.notes.map((n) => (
            <li key={n}>
              <em>{n}</em>
            </li>
          ))}
        </ul>
      )}
      {alert.contributing_doses.length > 0 && (
        <>
          <div>Counted doses:</div>
          <ul>
            {alert.contributing_doses.map((d) => (
              <li key={d.dose_event_id}>
                {d.product_name} — {d.amount_mg} mg at{" "}
                {new Date(d.taken_at).toLocaleString()}
              </li>
            ))}
          </ul>
        </>
      )}
      {alert.uncounted_doses.length > 0 && (
        <>
          <div>Not counted (total is incomplete):</div>
          <ul>
            {alert.uncounted_doses.map((d) => (
              <li key={`${d.dose_event_id}-${d.ingredient_name}`}>
                {d.product_name} — {d.ingredient_name} (
                {d.unmodeled_reason === "unmatched"
                  ? "ingredient not recognized"
                  : "strength unknown"}
                )
              </li>
            ))}
          </ul>
        </>
      )}
    </li>
  );
}

/** Last 24 hours of curves for SUBSTANCES, plus active alerts. */
async function fetchDashboard(): Promise<{
  curves: SubstanceCurve[];
  alerts: Alert[];
}> {
  const to = new Date();
  const from = new Date(to.getTime() - 24 * 60 * 60 * 1000);
  const [curves, alerts] = await Promise.all([
    Promise.all(
      SUBSTANCES.map((substance_id) =>
        fetchCurve(USER_ID, {
          substance_id,
          from: from.toISOString(),
          to: to.toISOString(),
          resolution: "15m",
        }),
      ),
    ),
    fetchAlerts(USER_ID),
  ]);
  return { curves, alerts };
}

function App() {
  const [curves, setCurves] = useState<SubstanceCurve[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [productId, setProductId] = useState(PRODUCTS[0].id);
  const [quantity, setQuantity] = useState(1);
  const [logging, setLogging] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);

  // State is set only in promise callbacks, so this is safe to run from an
  // effect. Callers that re-load set `loading` themselves.
  const load = useCallback(
    () =>
      fetchDashboard()
        .then(({ curves, alerts }) => {
          setCurves(curves);
          setAlerts(alerts);
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

  async function handleLogDose() {
    setLogging(true);
    setLogError(null);
    try {
      await logDose(USER_ID, {
        product_id: productId,
        taken_at: new Date().toISOString(),
        quantity,
      });
      setLoading(true);
      await load();
    } catch (e) {
      setLogError(errorMessage(e));
    } finally {
      setLogging(false);
    }
  }

  return (
    <div style={{ padding: "2rem" }}>
      <h1>Pharmakon</h1>

      <section>
        <h2>Log a dose</h2>
        <select value={productId} onChange={(e) => setProductId(e.target.value)}>
          {PRODUCTS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>{" "}
        <input
          type="number"
          min={1}
          step={1}
          value={quantity}
          onChange={(e) => setQuantity(Math.max(1, Number(e.target.value)))}
          style={{ width: "4rem" }}
          aria-label="Quantity"
        />{" "}
        <button onClick={handleLogDose} disabled={logging}>
          {logging ? "Logging…" : "Log dose"}
        </button>
        {logError && <p style={{ color: "red" }}>Couldn't log dose: {logError}</p>}
      </section>

      <section>
        <h2>Last 24 hours</h2>
        {loading && curves.length === 0 ? (
          <p>Loading…</p>
        ) : error ? (
          <p style={{ color: "red" }}>
            Couldn't reach the API: {error}{" "}
            <button onClick={reload}>Retry</button>
          </p>
        ) : (
          curves.map((curve) => (
            <ConcentrationChart key={curve.substance.id} curve={curve} />
          ))
        )}
      </section>

      <section>
        <h2>Alerts</h2>
        {!loading && !error && alerts.length === 0 && <p>No active alerts.</p>}
        <ul>
          {alerts.map((a) => (
            <AlertItem key={a.id} alert={a} />
          ))}
        </ul>
      </section>

      <footer>
        <small>
          Not a medical device. Estimates from a one-compartment,
          population-average model, for educational and demonstrative use
          only.
        </small>
      </footer>
    </div>
  );
}

export default App;
