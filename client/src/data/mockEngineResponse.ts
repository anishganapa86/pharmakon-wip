import type { EngineResponse } from "../types/contract";

export const mockEngineResponse: EngineResponse = {
  unit: "mg/L",
  curves: [
    {
      substance: { id: "caffeine", display_name: "Caffeine" },
      modelable: true,
      reason: null,
      unit: "mg/L",
      threshold_concentration_mg_per_l: null,
      peak: null,
      current: null,
      notes: [],
      points: [
        { t: "2026-09-25T08:00:00-04:00", concentration: 0 },
        { t: "2026-09-25T09:00:00-04:00", concentration: 4.2 },
        { t: "2026-09-25T10:00:00-04:00", concentration: 3.8 },
        { t: "2026-09-25T13:00:00-04:00", concentration: 2.1 },
        { t: "2026-09-25T18:00:00-04:00", concentration: 0.7 },
      ],
    },
    {
      substance: { id: "acetaminophen", display_name: "Acetaminophen" },
      modelable: true,
      reason: null,
      unit: "mg/L",
      threshold_concentration_mg_per_l: null,
      peak: null,
      current: null,
      notes: [],
      points: [
        { t: "2026-09-25T08:00:00-04:00", concentration: 0 },
        { t: "2026-09-25T09:00:00-04:00", concentration: 8.5 },
        { t: "2026-09-25T10:00:00-04:00", concentration: 7.9 },
        { t: "2026-09-25T13:00:00-04:00", concentration: 3.4 },
        { t: "2026-09-25T18:00:00-04:00", concentration: 0.4 },
      ],
    },
  ],
  alerts: [],
};