import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import { formatDateClock, tickFormatter, timeTicks } from "../time";
import type { SubstanceCurve } from "../types/contract";

export interface TimeWindow {
  fromMs: number;
  toMs: number;
  nowMs: number;
}

interface Props {
  curve: SubstanceCurve;
  window: TimeWindow;
}

function Notes({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <ul className="notes">
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

function SubstanceHeading({ curve }: { curve: SubstanceCurve }) {
  return (
    <>
      <h3>{curve.substance.display_name}</h3>
      {curve.substance.description && (
        <p className="chart-subtitle muted small">{curve.substance.description}</p>
      )}
    </>
  );
}

export function ConcentrationChart({ curve, window }: Props) {
  const name = curve.substance.display_name;

  // A documented limit of the model (e.g. zero-order alcohol), not an error.
  // Show why instead of an empty chart that reads as "zero exposure".
  if (!curve.modelable) {
    return (
      <div className="chart">
        <SubstanceHeading curve={curve} />
        <p>This substance can't be charted: {curve.reason}</p>
        <Notes notes={curve.notes} />
      </div>
    );
  }

  if (curve.points.length === 0) {
    return (
      <div className="chart">
        <SubstanceHeading curve={curve} />
        <p>No data points yet.</p>
        <Notes notes={curve.notes} />
      </div>
    );
  }

  // Numeric time so the axis is linear in time and shared across charts.
  const data = curve.points.map((p) => ({
    t: new Date(p.t).getTime(),
    concentration: p.concentration,
  }));

  const { fromMs, toMs, nowMs } = window;
  const threshold = curve.threshold_concentration_mg_per_l;
  const seriesName = `${name} (${curve.unit})`;

  return (
    <div className="chart">
      <SubstanceHeading curve={curve} />
      <div className="chart-plot">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={[fromMs, toMs]}
              ticks={timeTicks(fromMs, toMs)}
              tickFormatter={tickFormatter(fromMs, toMs)}
              tick={{ fontSize: 12, fill: "var(--text)" }}
              stroke="var(--border)"
            />
            <YAxis
              tick={{ fontSize: 12, fill: "var(--text)" }}
              stroke="var(--border)"
              width={56}
              label={{
                value: curve.unit,
                angle: -90,
                position: "insideLeft",
                fill: "var(--text)",
                fontSize: 12,
              }}
            />
            <Tooltip
              labelFormatter={(t) => formatDateClock(Number(t))}
              formatter={(v) => [
                typeof v === "number" ? `${v.toFixed(3)} mg/L` : String(v),
                name,
              ]}
              contentStyle={{
                background: "var(--bg)",
                border: "1px solid var(--border)",
                color: "var(--text-h)",
              }}
            />
            <Legend verticalAlign="top" height={28} />
            <ReferenceLine
              x={nowMs}
              stroke="var(--text)"
              strokeDasharray="2 4"
              label={{ value: "now", position: "insideTopRight", fill: "var(--text)", fontSize: 12 }}
            />
            <Line
              type="monotone"
              dataKey="concentration"
              stroke="var(--accent)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              name={seriesName}
            />
            {/* null for every substance at launch; the chart must work without it */}
            {threshold !== null && (
              <ReferenceLine
                y={threshold}
                stroke="#dc2626"
                strokeDasharray="4 4"
                label={{ value: "Published threshold", fill: "#dc2626", fontSize: 12 }}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <Notes notes={curve.notes} />
    </div>
  );
}
