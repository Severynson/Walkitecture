import type { Problem } from "./schema";

const TOKEN_KEY = "walkitecture.token";

/** Error body of every failed API call: {"error": {"code", "message", "problems"}}. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly problems: Problem[] = [],
  ) {
    super(message);
  }

  /** Message plus the validation problems, for display. */
  get details(): string {
    const extra = this.problems.map((p) => p.message).filter((m) => m && m !== this.message);
    return [this.message, ...extra].join(" ");
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: the session simply won't persist */
  }
}

/** Called when the server rejects our token (expired / user deleted). Set by AuthProvider. */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

type Query = Record<string, string | number | boolean | undefined>;

interface RequestOptions {
  method?: string;
  query?: Query;
  json?: unknown;
  form?: FormData;
  /** Don't treat 401 as an expired session (login/register forms). */
  anonymous?: boolean;
}

export async function api<T>(path: string, { method = "GET", query, json, form, anonymous }: RequestOptions = {}): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token && !anonymous) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form) {
    body = form;
  }

  let response: Response;
  try {
    response = await fetch(url, { method, headers, body });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the server. Check your connection and try again.");
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    const err = (data as { error?: { code?: string; message?: string; problems?: Problem[] } } | null)?.error;
    const detail = (data as { detail?: unknown } | null)?.detail;
    const message = err?.message ?? (typeof detail === "string" ? detail : `Request failed (HTTP ${response.status}).`);
    if (response.status === 401 && !anonymous) onUnauthorized?.();
    throw new ApiError(response.status, err?.code ?? `http_${response.status}`, message, err?.problems ?? []);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { error: { code: "bad_response", message: text.slice(0, 200) } };
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.details;
  if (error instanceof Error) return error.message;
  return String(error);
}
