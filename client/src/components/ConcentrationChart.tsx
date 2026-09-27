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
import type { SubstanceCurve } from "../types/contract";

interface Props {
  curve: SubstanceCurve;
}

function Notes({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <ul>
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

export function ConcentrationChart({ curve }: Props) {
  const name = curve.substance.display_name;

  // A documented limit of the model (e.g. zero-order alcohol), not an error.
  // Show why instead of an empty chart that reads as "zero exposure".
  if (!curve.modelable) {
    return (
      <div style={{ padding: "1rem" }}>
        <h3>{name}</h3>
        <p>This substance can't be charted: {curve.reason}</p>
        <Notes notes={curve.notes} />
      </div>
    );
  }

  if (curve.points.length === 0) {
    return (
      <div style={{ padding: "1rem" }}>
        <h3>{name}</h3>
        <p>No data points yet.</p>
        <Notes notes={curve.notes} />
      </div>
    );
  }

  // Recharts wants a flat array of plain objects, so we reshape the points
  const data = curve.points.map((p) => ({
    time: p.t,
    concentration: p.concentration,
  }));

  const threshold = curve.threshold_concentration_mg_per_l;

  return (
    <div>
      <div style={{ width: "100%", height: 400 }}>
        <h3>{name}</h3>
        <ResponsiveContainer>
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="time" tick={{ fontSize: 10 }} />
            <YAxis
              label={{ value: curve.unit, angle: -90, position: "insideLeft" }}
            />
            <Tooltip />
            <Legend />
            <Line
              type="monotone"
              dataKey="concentration"
              stroke="#8884d8"
              name={`${name} (${curve.unit})`}
            />
            {/* null for every substance at launch; the chart must work without it */}
            {threshold !== null && (
              <ReferenceLine
                y={threshold}
                stroke="red"
                strokeDasharray="4 4"
                label="Published threshold"
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <Notes notes={curve.notes} />
    </div>
  );
}
