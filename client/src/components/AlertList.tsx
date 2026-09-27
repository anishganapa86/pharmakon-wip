import { formatDateClock } from "../time";
import type { Alert, AlertSeverity } from "../types/contract";

/** Presentational only -- not sent by the server. Conservative and generic
 * by design: this is guidance about the alert mechanism, not medical advice
 * about the substance, so it doesn't need a registry citation. */
const WHAT_TO_DO: Record<AlertSeverity, string> = {
  urgent: "Stop taking this and contact a pharmacist or prescriber now.",
  caution: "Don't take more until the level drops. Ask a pharmacist if unsure.",
  info: "No action needed — for your awareness.",
};

function AlertItem({ alert }: { alert: Alert }) {
  return (
    <li className="alert">
      <div className="alert-head">
        <span className={`badge badge-${alert.severity}`}>{alert.severity}</span>
        {alert.substance && (
          <span className="alert-substance">{alert.substance.display_name}</span>
        )}
        <span className="alert-kind">{alert.kind.replace("_", " ")}</span>
      </div>
      <p>{alert.message}</p>
      {alert.substance?.threshold_explanation && (
        <p className="alert-why muted">
          <strong>Why this matters:</strong> {alert.substance.threshold_explanation}
        </p>
      )}
      <p className="alert-action muted">
        <strong>What to do:</strong> {WHAT_TO_DO[alert.severity]}
      </p>
      {alert.notes.length > 0 && (
        <ul className="notes">
          {alert.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {alert.contributing_doses.length > 0 && (
        <div className="dose-list">
          <div className="dose-list-label">Counted doses</div>
          <ul>
            {alert.contributing_doses.map((d) => (
              <li key={d.dose_event_id}>
                {d.product_name} · {d.amount_mg} mg ·{" "}
                {formatDateClock(new Date(d.taken_at).getTime())}
              </li>
            ))}
          </ul>
        </div>
      )}
      {alert.uncounted_doses.length > 0 && (
        <div className="dose-list">
          <div className="dose-list-label">Not counted (total is incomplete)</div>
          <ul>
            {alert.uncounted_doses.map((d) => (
              <li key={`${d.dose_event_id}-${d.ingredient_name}`}>
                {d.product_name} · {d.ingredient_name} (
                {d.unmodeled_reason === "unmatched"
                  ? "ingredient not recognized"
                  : "strength unknown"}
                ) · {formatDateClock(new Date(d.taken_at).getTime())}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

export function AlertList({ alerts }: { alerts: Alert[] }) {
  if (alerts.length === 0) return <p className="muted">No active alerts.</p>;
  return (
    <ul className="alerts">
      {alerts.map((a) => (
        <AlertItem key={a.id} alert={a} />
      ))}
    </ul>
  );
}
