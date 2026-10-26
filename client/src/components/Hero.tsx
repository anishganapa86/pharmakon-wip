import type { Alert } from "../types/contract";

interface HeroProps {
  alerts: Alert[];
  doseCount: number;
  onDetails: () => void;
}

const RANK: Record<Alert["severity"], number> = { urgent: 3, caution: 2, info: 1 };

function leadingAlert(alerts: Alert[]): Alert | null {
  const withTotals = alerts.filter(
    (a) => a.substance !== null && a.contributing_doses.length > 0,
  );
  if (withTotals.length === 0) return null;
  return [...withTotals].sort((a, b) => RANK[b.severity] - RANK[a.severity])[0];
}

export function Hero({ alerts, doseCount, onDetails }: HeroProps) {
  const lead = leadingAlert(alerts);
  if (lead && lead.substance) {
    const total = lead.contributing_doses.reduce((sum, d) => sum + d.amount_mg, 0);
    const doses = lead.contributing_doses.length;
    return (
      <section className="hero" aria-label="Summary">
        <p className="hero-label">{lead.substance.display_name}</p>
        <p className="hero-number">
          {Math.round(total).toLocaleString()} <span className="hero-unit">mg</span>
        </p>
        <div className="hero-meta">
          <span className={`dot dot-${lead.severity}`} />
          <span className="hero-status">
            {lead.severity}, {doses} {doses === 1 ? "dose" : "doses"}
          </span>
          <button type="button" className="button-link hero-details" onClick={onDetails}>
            Details
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className="hero" aria-label="Summary">
      <p className="hero-label">Doses logged</p>
      <p className="hero-number">{doseCount.toLocaleString()}</p>
    </section>
  );
}
