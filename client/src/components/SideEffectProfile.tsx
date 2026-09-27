import { useEffect, useState } from "react";
import { fetchSideEffectProfile } from "../api";
import type { SideEffectProfileResponse } from "../types/contract";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

interface Props {
  userId: string;
}

// Mount once per (userId, refreshKey): App.tsx remounts this component with a
// fresh `key` after logging a dose, the same convention Timeline uses, rather
// than re-running this effect -- so the initial state below (loading=true) is
// always correct and this never needs to set it from inside the effect.
export function SideEffectProfile({ userId }: Props) {
  const [data, setData] = useState<SideEffectProfileResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchSideEffectProfile(userId)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(errorMessage(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const shared = new Set(data?.shared_effects ?? []);

  return (
    <details className="collapsible">
      <summary>Side effects of your current regimen</summary>
      <div className="collapsible-body">
        {loading ? (
          <p className="muted">Loading…</p>
        ) : error ? (
          <p className="error">Couldn't load side effects: {error}</p>
        ) : !data || data.substances.length === 0 ? (
          <p className="muted">Log a dose to see potential side effects.</p>
        ) : (
          <>
            <p className="muted small">
              These are side effects listed on FDA drug labels for the
              substances you've logged. Not all will occur.
            </p>
            <ul className="side-effect-list">
              {data.substances.map((s) => (
                <li key={s.substance.id} className="side-effect-substance">
                  <div className="side-effect-head">
                    <strong>{s.substance.display_name}</strong>
                    <span className="muted small">
                      {s.current_level_mg_l !== null
                        ? `${s.current_level_mg_l.toFixed(3)} mg/L current`
                        : `${s.total_24h_mg} mg in the last 24 hours`}
                    </span>
                  </div>
                  <p className="muted small">
                    From{" "}
                    {s.contributing_products
                      .map((p) => p.product_name)
                      .join(", ")}
                  </p>
                  {s.side_effects.length === 0 ? (
                    <p className="muted small">
                      No documented side effects found.
                    </p>
                  ) : (
                    <ul className="effect-tags">
                      {s.side_effects.map((e) => (
                        <li
                          key={e.effect}
                          className={
                            shared.has(e.effect)
                              ? "effect-tag effect-tag-shared"
                              : "effect-tag"
                          }
                          title={e.source_label}
                        >
                          {e.effect}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </details>
  );
}
