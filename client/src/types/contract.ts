export interface ConcentrationPoint {
  timestamp: string;              // ISO 8601 with timezone, e.g. "2026-09-25T14:00:00-04:00"
  concentration_mg_l: number;
}

export interface SubstanceCurve {
  substance: string;              // e.g. "caffeine"
  safety_threshold_mg_l: number | null;
  points: ConcentrationPoint[];
}

export interface DuplicationAlert {
  substance: string;
  contributing_dose_ids: string[];
  message: string;
}

export interface EngineResponse {
  curves: SubstanceCurve[];
  alerts: DuplicationAlert[];
}