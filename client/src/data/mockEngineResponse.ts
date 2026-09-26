import type { EngineResponse } from "../types/contract";

export const mockEngineResponse: EngineResponse = {
  curves: [
    {
      substance: "caffeine",
      safety_threshold_mg_l: 15,
      points: [
        { timestamp: "2026-09-25T08:00:00-04:00", concentration_mg_l: 0 },
        { timestamp: "2026-09-25T09:00:00-04:00", concentration_mg_l: 4.2 },
        { timestamp: "2026-09-25T10:00:00-04:00", concentration_mg_l: 3.8 },
        { timestamp: "2026-09-25T13:00:00-04:00", concentration_mg_l: 2.1 },
        { timestamp: "2026-09-25T18:00:00-04:00", concentration_mg_l: 0.7 },
      ],
    },
    {
      substance: "acetaminophen",
      safety_threshold_mg_l: 25,
      points: [
        { timestamp: "2026-09-25T08:00:00-04:00", concentration_mg_l: 0 },
        { timestamp: "2026-09-25T09:00:00-04:00", concentration_mg_l: 8.5 },
        { timestamp: "2026-09-25T10:00:00-04:00", concentration_mg_l: 7.9 },
        { timestamp: "2026-09-25T13:00:00-04:00", concentration_mg_l: 3.4 },
        { timestamp: "2026-09-25T18:00:00-04:00", concentration_mg_l: 0.4 },
      ],
    },
  ],
  alerts: [],
};