// Thin client for the Pharmakon API. Shapes come from ./types/contract.

import type {
  Alert,
  AlertsActiveFilter,
  ApiError,
  CurveQuery,
  DoseRequest,
  DoseResponse,
  LoggedDoseEvent,
  ProductSearchResult,
  SubstanceCurve,
  SymptomCheckResponse,
} from "./types/contract";

const API_BASE = "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as ApiError;
      message = body.error?.message ?? message;
    } catch {
      // Non-JSON error body; keep the status line.
    }
    throw new Error(message);
  }
  return (await res.json()) as T;
}

export function fetchCurve(
  userId: string,
  query: CurveQuery,
): Promise<SubstanceCurve> {
  // URLSearchParams encodes the "+" in timezone offsets, as the contract requires.
  const params = new URLSearchParams({
    substance_id: query.substance_id,
    from: query.from,
    to: query.to,
    resolution: query.resolution ?? "15m",
  });
  return request(`/users/${encodeURIComponent(userId)}/curve?${params}`);
}

export function logDose(
  userId: string,
  dose: DoseRequest,
): Promise<DoseResponse> {
  return request(`/users/${encodeURIComponent(userId)}/doses`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(dose),
  });
}

export function fetchAlerts(
  userId: string,
  active: AlertsActiveFilter = "true",
): Promise<Alert[]> {
  const params = new URLSearchParams({ active });
  return request(`/users/${encodeURIComponent(userId)}/alerts?${params}`);
}

export function fetchDoses(userId: string): Promise<LoggedDoseEvent[]> {
  return request(`/users/${encodeURIComponent(userId)}/doses`);
}

export function searchProducts(
  q: string,
  signal?: AbortSignal,
): Promise<ProductSearchResult[]> {
  const params = new URLSearchParams({ q });
  return request(`/products/search?${params}`, { signal });
}

export function checkSymptom(symptom: string): Promise<SymptomCheckResponse> {
  return request("/symptom-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symptom }),
  });
}
