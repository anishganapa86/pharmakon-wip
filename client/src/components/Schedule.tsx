import { useState } from "react";
import { createSchedule, deleteSchedule, takeScheduled } from "../api";
import {
  daysLabel,
  DEFAULT_TIMES,
  FREQUENCY_LABELS,
  NOT_MODELED_NO_PK_DATA,
  STATUS_LABELS,
  timeOfDayLabel,
  WEEKDAYS,
} from "../labels";
import { formatClock, formatWhen } from "../time";
import type {
  ProductSearchResult,
  RecentProduct,
  ScheduledMedication,
  ScheduleFrequency,
} from "../types/contract";
import { ProductName } from "./ProductName";
import { ProductSearch } from "./ProductSearch";

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const FREQUENCIES = Object.keys(FREQUENCY_LABELS) as ScheduleFrequency[];

// app/api/food_guidance.py keys a single-ingredient finding by the
// substance's own display name, and a combination product's finding by the
// product name -- so a schedule card matches on either.
function hasFoodGuidance(m: ScheduledMedication, withFoodNames: Set<string>): boolean {
  if (withFoodNames.has(m.product_name.trim().toLowerCase())) return true;
  return m.substances.some(
    (s) => s.display_name && withFoodNames.has(s.display_name.trim().toLowerCase()),
  );
}

function nextDueText(m: ScheduledMedication): string {
  if (m.frequency === "as_needed") return "Take only when needed";
  if (m.next_due === null) return "Nothing scheduled";
  const ms = new Date(m.next_due).getTime();
  return m.status === "overdue"
    ? `Overdue since ${formatWhen(ms)}`
    : `Next: ${formatWhen(ms)}`;
}

function Card({
  med,
  busy,
  onTake,
  onRemove,
  withFood,
}: {
  med: ScheduledMedication;
  busy: boolean;
  onTake: () => void;
  onRemove: () => void;
  withFood: boolean;
}) {
  const days = daysLabel(med.days_of_week);
  const when = med.times.map(timeOfDayLabel).join(", ");
  return (
    <li className={`med-card med-card-${med.status}`}>
      <div className="med-card-head">
        <strong className="med-name">
          <ProductName name={med.product_name} />
        </strong>
        {/* Text as well as color: status is never color alone. */}
        <span className={`med-status med-status-${med.status}`}>
          {STATUS_LABELS[med.status]}
        </span>
        {withFood && (
          <span
            className="tag"
            title="A quoted FDA label sentence says to take this with food."
          >
            With food
          </span>
        )}
      </div>
      <p className="muted small med-when">
        {FREQUENCY_LABELS[med.frequency]}
        {when && ` · ${when}`}
        {days && ` · ${days}`}
      </p>
      <p className="med-next">{nextDueText(med)}</p>
      {med.last_taken && (
        <p className="muted small">
          Last taken: {formatWhen(new Date(med.last_taken).getTime())}
        </p>
      )}
      {med.today.length > 1 && (
        <ul className="med-slots">
          {med.today.map((s) => (
            <li key={s.scheduled_at} className={`med-slot med-slot-${s.status}`}>
              {formatClock(new Date(s.scheduled_at).getTime())}
              {": "}
              {s.status === "taken" ? "taken" : s.status.replace("_", " ")}
            </li>
          ))}
        </ul>
      )}
      <ul className="med-levels">
        {med.substances.map((s, i) => (
          <li key={`${s.name}-${i}`} className="small">
            <span className="muted">{s.display_name ?? s.name}: </span>
            {s.current_concentration === null ? (
              <span className="muted small">{NOT_MODELED_NO_PK_DATA}</span>
            ) : (
              <strong>
                {s.current_concentration.toFixed(2)} {s.unit}
              </strong>
            )}
          </li>
        ))}
      </ul>
      {med.notes && <p className="muted small">{med.notes}</p>}
      <div className="med-actions">
        <button
          className="control button"
          onClick={onTake}
          disabled={busy}
          type="button"
        >
          {busy ? "Logging…" : "Take now"}
        </button>
        <button className="button-link small" onClick={onRemove} type="button">
          Remove
        </button>
      </div>
    </li>
  );
}

function AddForm({
  recent,
  onSaved,
  onCancel,
  userId,
}: {
  recent: RecentProduct[];
  onSaved: () => void;
  onCancel: () => void;
  userId: string;
}) {
  const [product, setProduct] = useState<ProductSearchResult | null>(null);
  const [frequency, setFrequency] = useState<ScheduleFrequency>("daily");
  const [times, setTimes] = useState<string[]>(DEFAULT_TIMES.daily);
  const [days, setDays] = useState<string[]>([]);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pickFrequency(f: ScheduleFrequency) {
    setFrequency(f);
    setTimes(DEFAULT_TIMES[f]); // one picker per dose per day
    if (f !== "weekly") setDays([]);
  }

  const timesValid = times.every((t) => /^\d{2}:\d{2}$/.test(t));
  const daysValid = frequency !== "weekly" || days.length > 0;
  const canSave = product !== null && timesValid && daysValid && !saving;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!product || !canSave) return;
    setSaving(true);
    setError(null);
    try {
      await createSchedule(userId, {
        product_id: product.id,
        frequency,
        times,
        days_of_week: frequency === "weekly" ? days : undefined,
        notes: notes.trim() || undefined,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setSaving(false);
    }
  }

  return (
    <form className="med-form" onSubmit={save}>
      <ProductSearch selected={product} onSelect={setProduct} recent={recent} />

      <div className="log-form-row">
        <label className="med-field">
          <span className="muted small">How often</span>
          <select
            className="control"
            value={frequency}
            onChange={(e) => pickFrequency(e.target.value as ScheduleFrequency)}
          >
            {FREQUENCIES.map((f) => (
              <option key={f} value={f}>
                {FREQUENCY_LABELS[f]}
              </option>
            ))}
          </select>
        </label>
        {times.map((t, i) => (
          <label className="med-field" key={i}>
            <span className="muted small">
              {times.length > 1 ? `Time ${i + 1}` : "Time"}
            </span>
            <input
              className="control"
              type="time"
              value={t}
              required
              onChange={(e) =>
                setTimes((prev) =>
                  prev.map((p, j) => (j === i ? e.target.value : p)),
                )
              }
            />
          </label>
        ))}
      </div>

      {frequency === "weekly" && (
        <fieldset className="med-days">
          <legend className="muted small">Days</legend>
          {WEEKDAYS.map((d) => (
            <label key={d} className="med-day">
              <input
                type="checkbox"
                checked={days.includes(d)}
                onChange={(e) =>
                  setDays((prev) =>
                    e.target.checked ? [...prev, d] : prev.filter((x) => x !== d),
                  )
                }
              />
              {d[0].toUpperCase() + d.slice(1, 3)}
            </label>
          ))}
        </fieldset>
      )}
      {frequency === "every_other_day" && (
        <p className="muted small">Counts every second day, starting today.</p>
      )}
      {frequency === "as_needed" && (
        <p className="muted small">
          No reminders or overdue status. Take it from here to log it in one tap.
        </p>
      )}

      <input
        className="control"
        type="text"
        placeholder="Notes (optional)"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        aria-label="Notes"
      />

      <div className="med-actions">
        <button className="control button" type="submit" disabled={!canSave}>
          {saving ? "Saving…" : "Save medication"}
        </button>
        <button className="button-link" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p className="error">Couldn't save: {error}</p>}
    </form>
  );
}

interface Props {
  userId: string;
  medications: ScheduledMedication[] | null; // null while loading
  recent: RecentProduct[];
  /** Called after any change (a dose taken, a medication added or removed). */
  onChanged: () => void;
  interactionsNotice: boolean;
  onViewInteractions: () => void;
  /** Lowercased product names with a quoted take_with_food finding (see
   * app/api/food_guidance.py) -- drives the small "With food" chip. */
  withFoodNames: Set<string>;
}

/**
 * The primary surface for someone managing daily medications. A schedule is a
 * plan: nothing counts as taken until "Take now" (or manual logging) records a
 * real dose, and only recorded doses feed alerts.
 */
export function Schedule({
  userId,
  medications,
  recent,
  onChanged,
  interactionsNotice,
  onViewInteractions,
  withFoodNames,
}: Props) {
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null = follow the default (collapsed once everything is taken today).
  const [openOverride, setOpenOverride] = useState<boolean | null>(null);

  const meds = medications ?? [];
  const allDone =
    meds.length > 0 &&
    meds.every((m) => m.status === "taken_today" || m.status === "as_needed");
  const open = openOverride ?? !allDone;

  async function take(m: ScheduledMedication) {
    if (
      m.status === "taken_today" &&
      !window.confirm(
        `You've already taken ${m.product_name} for today's scheduled dose. Log another dose?`,
      )
    ) {
      return;
    }
    setBusyId(m.id);
    setError(null);
    try {
      await takeScheduled(userId, m.id);
      onChanged();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(m: ScheduledMedication) {
    if (!window.confirm(`Stop scheduling ${m.product_name}? Your history is kept.`)) {
      return;
    }
    setError(null);
    try {
      await deleteSchedule(userId, m.id);
      onChanged();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <section className="panel">
      <div className="med-header">
        <h2>Your medications</h2>
        {meds.length > 0 && (
          <button
            className="button-link"
            aria-expanded={open}
            onClick={() => setOpenOverride(!open)}
            type="button"
          >
            {open ? "Hide" : "Show"}
          </button>
        )}
      </div>

      {interactionsNotice && (
        <p className="warning">
          Interactions found in your regimen. See the{" "}
          <button className="button-link" onClick={onViewInteractions} type="button">
            Interactions tab
          </button>
          .
        </p>
      )}

      {!open && allDone && (
        <p className="muted">All of today's doses are taken.</p>
      )}

      {open && (
        <>
          {medications === null ? (
            <p className="muted">Loading…</p>
          ) : meds.length === 0 && !adding ? (
            <p className="muted">
              No medications scheduled yet. Add the ones you take regularly, then
              log each dose with one tap.
            </p>
          ) : (
            <ul className="med-list">
              {meds.map((m) => (
                <Card
                  key={m.id}
                  med={m}
                  busy={busyId === m.id}
                  onTake={() => void take(m)}
                  onRemove={() => void remove(m)}
                  withFood={hasFoodGuidance(m, withFoodNames)}
                />
              ))}
            </ul>
          )}
          {error && <p className="error">{error}</p>}
        </>
      )}

      {adding ? (
        <AddForm
          userId={userId}
          recent={recent}
          onCancel={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            setOpenOverride(null);
            onChanged();
          }}
        />
      ) : (
        <button
          className="control button med-add"
          type="button"
          onClick={() => setAdding(true)}
        >
          Add medication
        </button>
      )}

      {meds.length > 0 && open && (
        <p className="muted small">
          A schedule is a plan, not a record: a dose counts only once you take
          it here or log it, and alerts are based on those logged doses.
        </p>
      )}
    </section>
  );
}
