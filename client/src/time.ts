// Time-axis helpers for the curve charts. Everything here is local time,
// for display only; the API always gets ISO strings with an offset.

const HOUR_MS = 60 * 60 * 1000;
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// Candidate tick spacings in hours. 24 divides evenly by all of them, so ticks
// land on round clock times.
const STEPS_H = [1, 2, 3, 4, 6, 12, 24];

/** "3:05 PM" */
export function formatClock(ms: number): string {
  const d = new Date(ms);
  const h = d.getHours();
  const minutes = String(d.getMinutes()).padStart(2, "0");
  return `${h % 12 || 12}:${minutes} ${h < 12 ? "AM" : "PM"}`;
}

/** "Sep 26, 3:05 PM" */
export function formatDateClock(ms: number): string {
  const d = new Date(ms);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${formatClock(ms)}`;
}

/** "Sep 26, 2026" -- for grouping entries by day on the timeline, where the
 * span isn't bounded to one day the way the charts are. */
export function formatDateHeader(ms: number): string {
  const d = new Date(ms);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

/** Local-calendar-day grouping key. */
export function dayKey(ms: number): string {
  return new Date(ms).toDateString();
}

/** Tick label formatter for a window: adds the date once the span passes 12h. */
export function tickFormatter(fromMs: number, toMs: number) {
  return toMs - fromMs > 12 * HOUR_MS ? formatDateClock : formatClock;
}

/** At most `maxTicks` ticks on round local clock times inside [fromMs, toMs]. */
export function timeTicks(fromMs: number, toMs: number, maxTicks = 8): number[] {
  const spanH = (toMs - fromMs) / HOUR_MS;
  const stepH = STEPS_H.find((s) => spanH / s < maxTicks) ?? 24;

  const d = new Date(fromMs);
  d.setMinutes(0, 0, 0);
  while (d.getTime() < fromMs || d.getHours() % stepH !== 0) {
    d.setHours(d.getHours() + 1);
  }

  const ticks: number[] = [];
  while (d.getTime() <= toMs && ticks.length < maxTicks) {
    ticks.push(d.getTime());
    d.setHours(d.getHours() + stepH);
  }
  return ticks;
}

export { HOUR_MS };

/** "Today, 8:00 AM" / "Tomorrow, 8:00 AM" / "Sep 26, 8:00 AM" (local time). */
export function formatWhen(ms: number, nowMs: number = Date.now()): string {
  const days = Math.round(
    (new Date(ms).setHours(0, 0, 0, 0) - new Date(nowMs).setHours(0, 0, 0, 0)) /
      (24 * HOUR_MS),
  );
  if (days === 0) return `Today, ${formatClock(ms)}`;
  if (days === 1) return `Tomorrow, ${formatClock(ms)}`;
  if (days === -1) return `Yesterday, ${formatClock(ms)}`;
  return formatDateClock(ms);
}
