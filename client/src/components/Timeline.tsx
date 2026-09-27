import { useEffect, useRef, useState } from "react";
import { fetchTimeline } from "../api";
import {
  categoryColor,
  loggedIngredientLabel,
  TIMELINE_DOSE_NOT_MODELED,
} from "../labels";
import { dayKey, formatClock, formatDateHeader } from "../time";
import type { TimelineDoseEntry, TimelineEntry } from "../types/contract";

const PAGE_MS = 30 * 24 * 60 * 60 * 1000; // matches the API's default range

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function entryMs(e: TimelineEntry): number {
  return new Date(e.timestamp).getTime();
}

function DoseCard({ entry }: { entry: TimelineDoseEntry }) {
  const modeled = entry.ingredients.some((i) => i.unmodeled_reason === null);
  return (
    <li className="timeline-item timeline-item-dose">
      <div className="timeline-item-head">
        <span className="tag tag-source">Dose</span>
        <strong>{entry.product_name}</strong>
        <span className="muted timeline-time">{formatClock(entryMs(entry))}</span>
      </div>
      <p className="timeline-detail">
        {entry.quantity} × {entry.ingredients.map(loggedIngredientLabel).join(" · ")}
      </p>
      {!modeled && <p className="muted small">{TIMELINE_DOSE_NOT_MODELED}</p>}
    </li>
  );
}

function LogCard({ entry }: { entry: Extract<TimelineEntry, { type: "log" }> }) {
  const color = categoryColor(entry.category);
  return (
    <li
      className="timeline-item timeline-item-log"
      style={{ borderLeftColor: color }}
    >
      <div className="timeline-item-head">
        <span className="category-tag" style={{ background: color }}>
          {entry.category}
        </span>
        <span className="muted timeline-time">{formatClock(entryMs(entry))}</span>
      </div>
      <p className="timeline-detail">{entry.value}</p>
      {entry.body_location && (
        <p className="muted small">Location: {entry.body_location}</p>
      )}
      {entry.notes && <p className="muted small">{entry.notes}</p>}
    </li>
  );
}

interface Props {
  userId: string;
}

export function Timeline({ userId }: Props) {
  // Newest first, for display. The API returns oldest-first; this component
  // reverses it and pages further into the past as the user scrolls.
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  // The `to` boundary for the next page. Tracks the REQUESTED range, not the
  // timestamps of whatever entries came back -- a page can be sparse (or
  // empty in the middle) without meaning the window itself was exhausted.
  const nextToMs = useRef<number>(Date.now());
  const sentinelRef = useRef<HTMLLIElement>(null);

  async function fetchPage(toMs: number) {
    const fromMs = toMs - PAGE_MS;
    const page = await fetchTimeline(userId, {
      from: new Date(fromMs).toISOString(),
      to: new Date(toMs).toISOString(),
    });
    nextToMs.current = fromMs - 1; // next page starts just before this one
    return page;
  }

  // Runs once per mount. A refetch (e.g. after logging a new entry) happens
  // by remounting this component with a fresh `key`, not by re-running this
  // effect, so initial state (loading=true, entries=[]) is all this needs.
  useEffect(() => {
    let cancelled = false;
    fetchPage(nextToMs.current)
      .then((page) => {
        if (cancelled) return;
        setEntries([...page].reverse());
      })
      .catch((e: unknown) => !cancelled && setError(errorMessage(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetchPage closes over userId, stable per mount
  }, [userId]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) void loadMore();
      },
      { root: sentinel.closest(".timeline-scroll"), threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries.length, hasMore, loading]);

  async function loadMore() {
    if (loading || loadingMore || !hasMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await fetchPage(nextToMs.current);
      if (page.length === 0) {
        setHasMore(false);
      } else {
        setEntries((prev) => [...prev, ...[...page].reverse()]);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoadingMore(false);
    }
  }

  if (loading && entries.length === 0) {
    return <p className="muted">Loading…</p>;
  }
  if (error && entries.length === 0) {
    return <p className="error">Couldn't load your timeline: {error}</p>;
  }
  if (entries.length === 0) {
    return <p className="muted">Start logging to see your timeline.</p>;
  }

  const rows: React.ReactNode[] = [];
  let lastDay = "";
  for (const entry of entries) {
    const ms = entryMs(entry);
    const key = dayKey(ms);
    if (key !== lastDay) {
      rows.push(
        <li key={`header-${key}`} className="timeline-date-header">
          {formatDateHeader(ms)}
        </li>,
      );
      lastDay = key;
    }
    rows.push(
      entry.type === "dose" ? (
        <DoseCard key={`dose-${entry.id}`} entry={entry} />
      ) : (
        <LogCard key={`log-${entry.id}`} entry={entry} />
      ),
    );
  }

  return (
    <div className="timeline-scroll">
      <ul className="timeline">
        {rows}
        <li ref={sentinelRef} className="timeline-sentinel">
          {loadingMore && <span className="muted small">Loading more…</span>}
          {!loadingMore && error && entries.length > 0 && (
            <span className="error small">
              Couldn't load more: {error}{" "}
              <button className="button-link" onClick={() => void loadMore()}>
                Retry
              </button>
            </span>
          )}
          {!loadingMore && !error && !hasMore && (
            <span className="muted small">Start of your history.</span>
          )}
        </li>
      </ul>
    </div>
  );
}
