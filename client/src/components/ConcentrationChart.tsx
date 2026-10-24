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
      <h3>
        {curve.substance.display_name}
        {!curve.parameters_verified && (
          <span
            className="tag"
            title="Half-life and other values in our database haven't been checked against a cited source."
          >
            Unverified parameters
          </span>
        )}
      </h3>
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
  // `projected` (future scheduled doses added) shares the actual curve's grid,
  // so it is joined by timestamp and exists only from "now" on.
  const projectedAt = new Map(
    (curve.projected_points ?? []).map((p) => [
      new Date(p.t).getTime(),
      p.concentration,
    ]),
  );
  const data = curve.points.map((p) => {
    const t = new Date(p.t).getTime();
    return { t, concentration: p.concentration, projected: projectedAt.get(t) };
  });

  const { fromMs, toMs, nowMs } = window;
  const threshold = curve.threshold_concentration_mg_per_l;
  const seriesName = `${name} (${curve.unit})`;
  const projectedName = "Expected if taken as scheduled";

  return (
    <div className="chart">
      <SubstanceHeading curve={curve} />
      {curve.fidelity === "approximate" && (
        <p className="warning small chart-approximate">
          Approximate: amount left in the body (mg), not a blood concentration.
        </p>
      )}
      <div className="chart-plot">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            {/* Horizontal rules only: vertical gridlines compete with the
                "now" marker and the projected segment. */}
            <CartesianGrid
              vertical={false}
              strokeDasharray="2 6"
              stroke="var(--hair)"
            />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={[fromMs, toMs]}
              ticks={timeTicks(fromMs, toMs)}
              tickFormatter={tickFormatter(fromMs, toMs)}
              tick={{ fontSize: 11, fill: "var(--text)", fontFamily: "var(--mono)" }}
              tickLine={false}
              stroke="var(--hair)"
            />
            <YAxis
              tick={{ fontSize: 11, fill: "var(--text)", fontFamily: "var(--mono)" }}
              tickLine={false}
              stroke="var(--hair)"
              width={54}
              label={{
                value: curve.unit,
                angle: -90,
                position: "insideLeft",
                fill: "var(--text)",
                fontSize: 10,
                fontFamily: "var(--mono)",
              }}
            />
            <Tooltip
              labelFormatter={(t) => formatDateClock(Number(t))}
              formatter={(v, n) => [
                typeof v === "number" ? `${v.toFixed(3)} ${curve.unit}` : String(v),
                n === projectedName ? `${name} (expected)` : name,
              ]}
              contentStyle={{
                background: "#fff",
                border: "1px solid var(--border)",
                borderRadius: 2,
                color: "var(--text-h)",
                fontFamily: "var(--mono)",
                fontSize: 11,
              }}
              labelStyle={{ color: "var(--text)", marginBottom: 4 }}
            />
            <Legend
              verticalAlign="top"
              height={26}
              iconSize={8}
              wrapperStyle={{
                fontFamily: "var(--mono)",
                fontSize: 10,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                color: "var(--text)",
              }}
            />
            <ReferenceLine
              x={nowMs}
              stroke="var(--text)"
              strokeDasharray="2 4"
              label={{
                value: "now",
                position: "insideTopRight",
                fill: "var(--text)",
                fontSize: 10,
                fontFamily: "var(--mono)",
              }}
            />
            <Line
              type="monotone"
              dataKey="concentration"
              stroke="var(--accent)"
              strokeWidth={2.25}
              dot={false}
              isAnimationActive={false}
              name={seriesName}
            />
            {projectedAt.size > 0 && (
              <Line
                type="monotone"
                dataKey="projected"
                stroke="var(--accent)"
                strokeWidth={2.25}
                strokeDasharray="6 4"
                strokeOpacity={0.7}
                dot={false}
                isAnimationActive={false}
                name={projectedName}
              />
            )}
            {/* null for every substance at launch; the chart must work without it */}
            {threshold !== null && (
              <ReferenceLine
                y={threshold}
                stroke="#dc2626"
                strokeDasharray="4 4"
                label={{
                  value: "Published threshold",
                  fill: "#dc2626",
                  fontSize: 10,
                  fontFamily: "var(--mono)",
                }}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <Notes notes={curve.notes} />
    </div>
  );
}
