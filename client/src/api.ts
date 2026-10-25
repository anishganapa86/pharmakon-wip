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
  FoodGuidanceResponse,
  InteractionCheckResponse,
  LoggedDoseEvent,
  MyProductEntry,
  MyProductRequest,
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
import type { Survey, SurveyAnswers, SurveyStatus } from "./survey";

const API_BASE = (
  (import.meta.env.VITE_API_BASE as string | undefined) || "http://localhost:8000"
).replace(/\/+$/, "");

export const LOGIN_PAGE = "/login.html";

export interface AuthUser {
  id: string;
  email: string;
  survey_status: SurveyStatus;
}

export class ApiRequestError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

export function redirectToLogin(): void {
  window.location.replace(LOGIN_PAGE);
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options: { redirectOn401?: boolean } = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...init, credentials: "include" });
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") throw e;
    throw new Error(
      "Couldn't reach the Pharmakon server. Check that it is running, then try again.",
      { cause: e },
    );
  }
  if (res.status === 401 && options.redirectOn401 !== false) {
    redirectToLogin();
    throw new ApiRequestError("Your session has ended. Please log in again.", 401);
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as ApiError;
      message = body.error?.message ?? message;
    } catch {
      // Non-JSON error body; keep the status line.
    }
    throw new ApiRequestError(message, res.status);
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

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatAnswer {
  reply: string;
  suggested_tab: string | null;
}

export function sendChat(userId: string, messages: ChatTurn[]): Promise<ChatAnswer> {
  return request(`/users/${encodeURIComponent(userId)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: messages.slice(-10) }),
  });
}

export interface ScanIngredient {
  name: string;
  strength: string | null;
  unit: string | null;
  role: "active" | "inactive" | "unknown";
}

export interface ScanResult {
  product_name: string | null;
  brand: string | null;
  product_type: "oral_medication" | "supplement" | "topical" | "food_drink" | "unknown";
  ingredients: ScanIngredient[];
  label_text_quality: "clear" | "partial" | "unreadable";
  notes: string | null;
}

export function scanLabel(userId: string, photo: Blob): Promise<ScanResult> {
  return request(`/users/${encodeURIComponent(userId)}/scan`, {
    method: "POST",
    headers: { "Content-Type": "image/jpeg" },
    body: photo,
  });
}

export function matchIngredients(names: string[]): Promise<{
  results: { name: string; matched: boolean; substance_id: string | null }[];
}> {
  return request("/ingredients/match", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
}

export function fetchSurvey(userId: string): Promise<Survey> {
  return request(`/users/${encodeURIComponent(userId)}/survey`);
}

export function saveSurvey(
  userId: string,
  body: SurveyAnswers & { status: "completed" | "skipped" },
): Promise<Survey> {
  return request(`/users/${encodeURIComponent(userId)}/survey`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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

// --- "My products" --------------------------------------------------------------

export function createMyProduct(
  userId: string,
  body: MyProductRequest,
): Promise<MyProductEntry> {
  return request(`/users/${encodeURIComponent(userId)}/my-products`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchMyProducts(userId: string): Promise<MyProductEntry[]> {
  return request(`/users/${encodeURIComponent(userId)}/my-products`);
}

export function deleteMyProduct(
  userId: string,
  productId: string,
): Promise<void> {
  return request(
    `/users/${encodeURIComponent(userId)}/my-products/${encodeURIComponent(productId)}`,
    { method: "DELETE" },
  );
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

// --- Food, drink, and timing guidance --------------------------------------------

export function checkFoodGuidance(names: string[]): Promise<FoodGuidanceResponse> {
  return request("/food-guidance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
}

export function fetchFoodGuidance(userId: string): Promise<FoodGuidanceResponse> {
  return request(`/users/${encodeURIComponent(userId)}/food-guidance`);
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

export async function fetchMe(): Promise<AuthUser | null> {
  try {
    return await request<AuthUser>("/auth/me", undefined, { redirectOn401: false });
  } catch (e) {
    if (e instanceof ApiRequestError && e.status === 401) return null;
    throw e;
  }
}

async function clearBrowserCaches(): Promise<void> {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.controller?.postMessage("clear-caches");
  }
  if ("caches" in window) {
    const names = await caches.keys();
    await Promise.all(names.map((name) => caches.delete(name)));
  }
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" }, { redirectOn401: false });
  } finally {
    await clearBrowserCaches().catch(() => undefined);
  }
}
