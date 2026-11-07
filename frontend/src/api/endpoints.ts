// One typed function per backend route the app uses (see contracts/openapi.json).
import { api } from "./client";
import type {
  ClassifyResponse,
  EditCommand,
  EditHistoryItem,
  EditResponse,
  HealthResponse,
  InterpretResponse,
  ParseResponse,
  PlanDetail,
  PlanSummary,
  ReviewResult,
  SceneResponse,
  TokenResponse,
  UserOut,
} from "./schema";

export const health = () => api<HealthResponse>("/api/health", { anonymous: true });

export const login = (username: string, password: string) =>
  api<TokenResponse>("/api/auth/login", { method: "POST", json: { username, password }, anonymous: true });
export const register = (username: string, password: string) =>
  api<TokenResponse>("/api/auth/register", { method: "POST", json: { username, password }, anonymous: true });
export const me = () => api<UserOut>("/api/auth/me");

export const listPlans = () => api<PlanSummary[]>("/api/plans", { query: { limit: 100 } });
export const getPlan = (id: string) => api<PlanDetail>(`/api/plans/${id}`);
export const deletePlan = (id: string) => api<void>(`/api/plans/${id}`, { method: "DELETE" });
export const createDemoPlan = () => api<ParseResponse>("/api/plans/demo", { method: "POST" });
export function uploadPlan(file: File) {
  const form = new FormData();
  form.append("file", file);
  return api<PlanDetail>("/api/plans", { method: "POST", form, query: { parse: true } });
}

export const getScene = (id: string) => api<SceneResponse>(`/api/plans/${id}/scene`);
export const applyEdit = (id: string, command: EditCommand, baseVersion: number) =>
  api<EditResponse>(`/api/plans/${id}/edits`, { method: "POST", json: { command, baseVersion, source: "ui" } });
export const undo = (id: string) => api<EditResponse>(`/api/plans/${id}/undo`, { method: "POST" });
export const editHistory = (id: string) => api<EditHistoryItem[]>(`/api/plans/${id}/edits`);

export const interpret = (body: {
  text: string;
  planId: string;
  selectedId?: string | null;
  cameraForward?: [number, number] | null;
  baseVersion?: number;
}) => api<InterpretResponse>("/api/interpret-edit", { method: "POST", json: { ...body, apply: true } });

export const classify = (id: string) =>
  api<ClassifyResponse>(`/api/plans/${id}/classify`, { method: "POST", query: { apply: true }, json: {} });
export const review = (id: string) => api<ReviewResult>(`/api/plans/${id}/review`, { method: "POST", query: { apply: true } });
