import { formatDateClock } from "../time";
import type {
  Alert,
  AlertSeverity,
  DuplicateIngredientFinding,
} from "../types/contract";

/** Presentational only -- not sent by the server. Conservative and generic
 * by design: this is guidance about the alert mechanism, not medical advice
 * about the substance, so it doesn't need a registry citation. */
const WHAT_TO_DO: Record<AlertSeverity, string> = {
  urgent: "Don't take more of this until you've spoken with a pharmacist or prescriber.",
  caution: "Hold off on more of this for now. Ask a pharmacist if you're unsure.",
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
        {alert.substance?.description && (
          <span className="alert-substance-description muted small">
            {alert.substance.description}
          </span>
        )}
        <span className="alert-kind">{alert.kind.replace("_", " ")}</span>
      </div>
      <p>{alert.message}</p>
      {alert.substance?.threshold_explanation && (
        <p className="alert-why muted">
          <strong>Why this matters:</strong> {alert.substance.threshold_explanation}
          {!alert.substance.verified && (
            <>
              {" "}
              <span className="tag">Unverified reference</span>
            </>
          )}
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

function DuplicateAlertItem({
  finding,
  onOpenInteractions,
}: {
  finding: DuplicateIngredientFinding;
  onOpenInteractions: () => void;
}) {
  return (
    <li className="duplicate-alert">
      <p className="finding-status">
        <span className="dot dot-info" />
        <span className="tag">Same ingredient</span>
      </p>
      <p>{finding.message}</p>
      <button className="button-link" onClick={onOpenInteractions} type="button">
        Open Interactions
      </button>
    </li>
  );
}

interface AlertListProps {
  alerts: Alert[];
  duplicates?: DuplicateIngredientFinding[];
  onOpenInteractions?: () => void;
}

export function AlertList({
  alerts,
  duplicates = [],
  onOpenInteractions,
}: AlertListProps) {
  if (alerts.length === 0 && duplicates.length === 0) {
    return (
      <>
        <p className="muted">No active alerts.</p>
        {onOpenInteractions && (
          <p className="muted small">
            Alerts appear here once the doses you log add up. Duplicate ingredients
            show on Interactions.
          </p>
        )}
      </>
    );
  }
  return (
    <ul className="alerts">
      {alerts.map((a) => (
        <AlertItem key={a.id} alert={a} />
      ))}
      {onOpenInteractions &&
        duplicates.map((d) => (
          <DuplicateAlertItem
            key={d.ingredient_name}
            finding={d}
            onOpenInteractions={onOpenInteractions}
          />
        ))}
    </ul>
  );
}
