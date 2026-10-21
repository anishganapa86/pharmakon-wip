import { useState } from "react";
import { checkSymptom } from "../api";
import { formatDateClock } from "../time";
import type { SymptomCheckResponse } from "../types/contract";
import { LoadingNote, UncheckedReasons } from "./LoadingNote";

export function SymptomCheck() {
  const [symptom, setSymptom] = useState("");
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<SymptomCheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!symptom.trim()) return;
    setChecking(true);
    setError(null);
    try {
      setResult(await checkSymptom(symptom.trim()));
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <form className="log-row" onSubmit={handleSubmit}>
        <input
          className="control grow"
          type="text"
          placeholder="Describe a symptom, e.g. headache"
          value={symptom}
          onChange={(e) => setSymptom(e.target.value)}
          aria-label="Symptom"
          maxLength={100}
        />
        <button
          className="control button"
          type="submit"
          disabled={!symptom.trim() || checking}
        >
          {checking ? "Checking…" : "Check"}
        </button>
      </form>
      {checking && <LoadingNote label="Checking FDA labels…" />}
      {error && <p className="error">Couldn't check: {error}</p>}
      {result && (
        <div className="symptom-result">
          <UncheckedReasons reasons={result.unchecked_reason} />
          <p className={result.matches.length ? "symptom-summary" : "muted"}>
            {result.message}
          </p>
          {result.matches.length > 0 && (
            <ul className="alerts">
              {result.matches.map((m) => (
                <li key={m.substance.id} className="alert">
                  <div className="alert-head">
                    <span
                      className={`badge badge-${m.threshold_status === "above" ? "urgent" : "caution"}`}
                    >
                      {m.threshold_status}
                    </span>
                    <span className="alert-substance">
                      {m.substance.display_name}
                    </span>
                    <span className={`source source-${m.source}`}>
                      {m.source === "openfda_label" ? "FDA label" : "Local reference"}
                    </span>
                  </div>
                  <p>{m.message}</p>
                  {m.current_level_mg_l !== null && (
                    <p className="muted small">
                      Estimated current level: {m.current_level_mg_l.toFixed(3)} mg/L
                    </p>
                  )}
                  <div className="dose-list">
                    <div className="dose-list-label">Doses in the last 24 hours</div>
                    <ul>
                      {m.contributing_doses.map((d) => (
                        <li key={d.dose_event_id}>
                          {d.product_name} · {d.amount_mg} mg ·{" "}
                          {formatDateClock(new Date(d.taken_at).getTime())}
                        </li>
                      ))}
                    </ul>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}
