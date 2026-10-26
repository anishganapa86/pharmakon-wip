import type { Alert } from "../types/contract";

interface HeroProps {
  alerts: Alert[];
  doseCount: number;
  highlighted?: boolean;
  onDetails: (alertId: string) => void;
}

const RANK: Record<Alert["severity"], number> = { urgent: 3, caution: 2, info: 1 };

function leadingAlert(alerts: Alert[]): Alert | null {
  const withTotals = alerts.filter(
    (a) => a.substance !== null && a.contributing_doses.length > 0,
  );
  if (withTotals.length === 0) return null;
  return [...withTotals].sort((a, b) => RANK[b.severity] - RANK[a.severity])[0];
}

export function Hero({ alerts, doseCount, highlighted = false, onDetails }: HeroProps) {
  const lead = leadingAlert(alerts);
  const className = highlighted ? "hero hero-highlight" : "hero";
  if (lead && lead.substance) {
    const total = lead.contributing_doses.reduce((sum, d) => sum + d.amount_mg, 0);
    const doses = lead.contributing_doses.length;
    return (
      <section className={className} aria-label="Summary">
        <p className="hero-label">{lead.substance.display_name}</p>
        <p className="hero-number">
          {Math.round(total).toLocaleString()} <span className="hero-unit">mg</span>
        </p>
        <div className="hero-meta">
          <span className={`dot dot-${lead.severity}`} />
          <span className="hero-status">
            {lead.severity}, {doses} {doses === 1 ? "dose" : "doses"}
          </span>
          <button type="button" className="button-link hero-details" onClick={() => onDetails(lead.id)}>
            Details
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className={className} aria-label="Summary">
      <p className="hero-label">Doses logged</p>
      <p className="hero-number">{doseCount.toLocaleString()}</p>
    </section>
  );
}
