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

export function ConcentrationChart({ curve }: Props) {
  if (curve.points.length === 0) {
    return (
      <div style={{ padding: "1rem" }}>
        <h3>{curve.substance}</h3>
        <p>No data points yet.</p>
      </div>
    );
  }

  // Recharts wants a flat array of plain objects, so we reshape the points
  const data = curve.points.map((p) => ({
    time: p.timestamp,
    concentration: p.concentration_mg_l,
  }));

  return (
    <div style={{ width: "100%", height: 400 }}>
      <h3>{curve.substance}</h3>
      <ResponsiveContainer>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="time" tick={{ fontSize: 10 }} />
          <YAxis label={{ value: "mg/L", angle: -90, position: "insideLeft" }} />
          <Tooltip />
          <Legend />
          <Line
            type="monotone"
            dataKey="concentration"
            stroke="#8884d8"
            name={`${curve.substance} (mg/L)`}
          />
          {curve.safety_threshold_mg_l !== null && (
            <ReferenceLine
              y={curve.safety_threshold_mg_l}
              stroke="red"
              strokeDasharray="4 4"
              label="Safety threshold"
            />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}