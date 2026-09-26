import { ConcentrationChart } from "./components/ConcentrationChart";
import { mockEngineResponse } from "./data/mockEngineResponse";

function App() {
  const { curves } = mockEngineResponse;

  return (
    <div style={{ padding: "2rem" }}>
      <h1>Pharmakon</h1>
      {curves.length === 0 ? (
        <p>No substances logged yet.</p>
      ) : (
        curves.map((curve) => (
          <ConcentrationChart key={curve.substance} curve={curve} />
        ))
      )}
    </div>
  );
}

export default App;