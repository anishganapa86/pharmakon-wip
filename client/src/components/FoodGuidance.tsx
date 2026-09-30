import { useEffect, useState } from "react";
import { checkFoodGuidance, fetchFoodGuidance } from "../api";
import { formatClock } from "../time";
import type {
  FoodGuidanceClass,
  FoodGuidanceFinding,
  FoodGuidanceResponse,
  FoodGuidanceTimelineEntry,
} from "../types/contract";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function parseNames(text: string): string[] {
  return text
    .split(",")
    .map((n) => n.trim())
    .filter((n) => n.length > 0);
}

const CLASS_LABELS: Record<FoodGuidanceClass, string> = {
  take_with_food: "Take with food",
  take_on_empty_stomach: "Take on an empty stomach",
  avoid_item: "Avoid",
  separate_from: "Keep apart",
  other: "Other",
};

function FindingCard({ finding }: { finding: FoodGuidanceFinding }) {
  return (
    <div className="skincare-flag skincare-flag-caution">
      <p>
        <strong>{finding.name}</strong>
        {finding.is_product && (
          <span className="muted small"> (combination product)</span>
        )}
        {" — "}
        <span className="tag">{CLASS_LABELS[finding.class]}</span>
      </p>
      <p className="muted small">"{finding.quote}"</p>
      {finding.window && (
        <p className="muted small">
          {finding.window.before_hours}h before / {finding.window.after_hours}h
          after, per the label
        </p>
      )}
      <p className="muted small">
        <span className="tag tag-source">
          {finding.source_type === "fda_label" ? "FDA label" : "Curated reference"}
        </span>
        {" · "}
        {finding.label_field}
        {!finding.verified && (
          <>
            {" · "}
            <span className="tag">Unverified reference</span>
          </>
        )}
      </p>
    </div>
  );
}

function timelineLabel(entry: FoodGuidanceTimelineEntry): string {
  const who = CLASS_LABELS[entry.class];
  if (entry.type === "dose_marker") {
    return `${who} — ${entry.name} (${entry.source})`;
  }
  if (entry.type === "window") {
    return `${who} — ${entry.name}`;
  }
  return `${who} — ${entry.name} (ongoing)`;
}

function timelineSortKey(entry: FoodGuidanceTimelineEntry): string {
  return entry.at ?? entry.from ?? "";
}

function TimelineStrip({ entries }: { entries: FoodGuidanceTimelineEntry[] }) {
  const timed = entries.filter((e) => e.type !== "ongoing");
  const ongoing = entries.filter((e) => e.type === "ongoing");
  const sorted = [...timed].sort((a, b) =>
    timelineSortKey(a).localeCompare(timelineSortKey(b)),
  );

  if (entries.length === 0) return null;

  return (
    <div className="timeline-strip">
      <p className="muted small">Next 24 hours</p>
      <ul className="effect-tags">
        {sorted.map((entry, i) => (
          <li key={`timed-${i}`} className="effect-tag" title={entry.quote}>
            {entry.at && `${formatClock(new Date(entry.at).getTime())} — `}
            {entry.from &&
              entry.to &&
              `${formatClock(new Date(entry.from).getTime())}–` +
                `${formatClock(new Date(entry.to).getTime())} — `}
            {timelineLabel(entry)}
          </li>
        ))}
        {ongoing.map((entry, i) => (
          <li key={`ongoing-${i}`} className="effect-tag" title={entry.note}>
            {timelineLabel(entry)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Results({ result }: { result: FoodGuidanceResponse }) {
  return (
    <>
      {result.unchecked_reason.length > 0 && (
        <ul className="effect-tags">
          {result.unchecked_reason.map((reason, i) => (
            <li key={i} className="effect-tag">
              {reason}
            </li>
          ))}
        </ul>
      )}
      {result.guidance.length === 0 ? (
        <p className="muted">{result.message}</p>
      ) : (
        <>
          <TimelineStrip entries={result.timeline} />
          {result.guidance.map((finding, i) => (
            <FindingCard key={i} finding={finding} />
          ))}
        </>
      )}
    </>
  );
}

interface Props {
  userId: string;
}

export function FoodGuidance({ userId }: Props) {
  const [regimenResult, setRegimenResult] = useState<FoodGuidanceResponse | null>(
    null,
  );
  const [regimenLoading, setRegimenLoading] = useState(true);
  const [regimenError, setRegimenError] = useState<string | null>(null);

  const [namesText, setNamesText] = useState("");
  const [checking, setChecking] = useState(false);
  const [manualResult, setManualResult] = useState<FoodGuidanceResponse | null>(
    null,
  );
  const [manualError, setManualError] = useState<string | null>(null);

  // Mounts once per tab visit, same convention as Skincare/Interactions:
  // App.tsx only renders this while the Food & drink tab is active.
  useEffect(() => {
    let cancelled = false;
    fetchFoodGuidance(userId)
      .then((r) => !cancelled && setRegimenResult(r))
      .catch((e: unknown) => !cancelled && setRegimenError(errorMessage(e)))
      .finally(() => !cancelled && setRegimenLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const names = parseNames(namesText);

  async function handleCheck(e: React.FormEvent) {
    e.preventDefault();
    if (names.length === 0) return;
    setChecking(true);
    setManualError(null);
    try {
      setManualResult(await checkFoodGuidance(names));
    } catch (err) {
      setManualResult(null);
      setManualError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <p className="warning timeline-disclaimer">
        These are instructions quoted from FDA labels. They are not medical
        advice. Ask a pharmacist how they apply to you.
      </p>

      <section className="panel">
        <h2>Your regimen</h2>
        {regimenLoading ? (
          <p className="muted">Loading…</p>
        ) : regimenError ? (
          <p className="error">Couldn't check: {regimenError}</p>
        ) : (
          regimenResult && <Results result={regimenResult} />
        )}
      </section>

      <section className="panel">
        <h2>Check a medication</h2>
        <form className="skincare-form" onSubmit={handleCheck}>
          <input
            className="control grow"
            type="text"
            placeholder="Names, comma-separated (e.g. ibuprofen, warfarin)"
            value={namesText}
            onChange={(e) => setNamesText(e.target.value)}
            aria-label="Medication names"
          />
          <div className="log-row">
            <button
              className="control button"
              type="submit"
              disabled={names.length === 0 || checking}
            >
              {checking ? "Checking…" : "Check"}
            </button>
          </div>
        </form>
        {manualError && <p className="error">Couldn't check: {manualError}</p>}
      </section>

      {manualResult && (
        <section className="panel">
          <h2>Results</h2>
          <Results result={manualResult} />
        </section>
      )}
    </>
  );
}
