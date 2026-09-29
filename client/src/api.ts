// Thin client for the Pharmakon API. Shapes come from ./types/contract.

import type {
  Alert,
  AlertsActiveFilter,
  ApiError,
  CurveQuery,
  DailyLogEntry,
  DailyLogRequest,
  DoseRequest,
  DoseResponse,
  InteractionCheckResponse,
  LoggedDoseEvent,
  ProductSearchResponse,
  ProductSearchResult,
  ScheduledMedication,
  ScheduleRequest,
  SideEffectProfileResponse,
  SkincareAlternativesResponse,
  SkincareCheckResponse,
  SkincareProductRequest,
  SubstanceCurve,
  SymptomCheckResponse,
  TakeScheduledResponse,
  TimelineEntry,
  TimelineQuery,
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
  if (res.status === 204) return undefined as T; // no body, e.g. DELETE
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
): Promise<ProductSearchResponse> {
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

export function createLog(
  userId: string,
  entry: DailyLogRequest,
): Promise<DailyLogEntry> {
  return request(`/users/${encodeURIComponent(userId)}/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(entry),
  });
}

export function deleteLog(userId: string, logId: string): Promise<void> {
  return request(
    `/users/${encodeURIComponent(userId)}/log/${encodeURIComponent(logId)}`,
    { method: "DELETE" },
  );
}

export function fetchSideEffectProfile(
  userId: string,
): Promise<SideEffectProfileResponse> {
  return request(`/users/${encodeURIComponent(userId)}/side-effect-profile`);
}

export function checkSkincare(
  products: SkincareProductRequest[],
): Promise<SkincareCheckResponse> {
  return request("/skincare-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ products }),
  });
}

export function fetchSkincareCheck(userId: string): Promise<SkincareCheckResponse> {
  return request(`/users/${encodeURIComponent(userId)}/skincare-check`);
}

export function checkSkincareAlternatives(
  products: SkincareProductRequest[],
): Promise<SkincareAlternativesResponse> {
  return request("/skincare-alternatives", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ products }),
  });
}

export function createUserEnteredProduct(
  name: string,
  ingredients: string[],
): Promise<ProductSearchResult> {
  return request("/products/user-entered", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ingredients }),
  });
}

export function fetchTimeline(
  userId: string,
  query: TimelineQuery = {},
): Promise<TimelineEntry[]> {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  const qs = params.toString();
  return request(
    `/users/${encodeURIComponent(userId)}/timeline${qs ? `?${qs}` : ""}`,
  );
}

// --- Medication interaction checker ---------------------------------------------

export function checkInteractions(names: string[]): Promise<InteractionCheckResponse> {
  return request("/interaction-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
}

export function fetchInteractionCheck(
  userId: string,
): Promise<InteractionCheckResponse> {
  return request(`/users/${encodeURIComponent(userId)}/interaction-check`);
}

// --- Medication schedule -------------------------------------------------------

const scheduleBase = (userId: string) =>
  `/users/${encodeURIComponent(userId)}/schedule`;

export function fetchSchedule(userId: string): Promise<ScheduledMedication[]> {
  return request(scheduleBase(userId));
}

export function createSchedule(
  userId: string,
  body: ScheduleRequest,
): Promise<ScheduledMedication> {
  return request(scheduleBase(userId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function deleteSchedule(userId: string, scheduleId: string): Promise<void> {
  return request(`${scheduleBase(userId)}/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
  });
}

/** Logs one unit of the scheduled product as a real dose. */
export function takeScheduled(
  userId: string,
  scheduleId: string,
): Promise<TakeScheduledResponse> {
  return request(`${scheduleBase(userId)}/${encodeURIComponent(scheduleId)}/take`, {
    method: "POST",
  });
}
