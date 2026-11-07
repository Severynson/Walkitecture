// Friendly aliases over the generated contract (src/api/types.ts <- contracts/openapi.json).
import type { components } from "./types";

type S = components["schemas"];

export type TokenResponse = S["TokenResponse"];
export type UserOut = S["UserOut"];
export type HealthResponse = S["HealthResponse"];
export type PlanSummary = S["PlanSummary"];
export type PlanDetail = S["PlanDetail"];
export type ParseResponse = S["ParseResponse"];
export type SceneResponse = S["SceneResponse"];
export type Building = S["Building"];
export type Wall = S["Wall"];
export type Opening = S["Opening"];
export type FurnitureItem = S["FurnitureItem"];
export type EditRequest = S["EditRequest"];
export type EditCommand = EditRequest["command"];
export type EditResponse = S["EditResponse"];
export type EditHistoryItem = S["EditHistoryItem"];
export type InterpretRequest = S["InterpretRequest"];
export type InterpretResponse = S["InterpretResponse"];
export type ClassifyResponse = S["ClassifyResponse"];
export type ReviewResult = S["ReviewResult"];
export type Problem = S["Problem"];
