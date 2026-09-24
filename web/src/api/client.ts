import createClient from "openapi-fetch";

import { ApiError, toApiError } from "./problem";
import type { paths } from "./schema";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * What the console's session layer plugs into the fetch wrapper. The CSRF token
 * lives in memory only (docs/adr/0009): it is never written to any storage a
 * script can read back later.
 */
export interface ClientHooks {
  /** CSRF token of the current web or setup session, or null when signed out. */
  getCsrfToken: () => string | null;
  /** Re-reads `GET /auth/web/session` once after `403 csrf_failed`; returns the fresh token. */
  refreshCsrfToken: () => Promise<string | null>;
  /** Called on `401`, so the guard can send the user back to sign-in. */
  onUnauthorized: () => void;
  /** Opens the re-authentication dialog on `403 reauth_required`; true when it succeeded. */
  requestReauth: () => Promise<boolean>;
}

const noHooks: ClientHooks = {
  getCsrfToken: () => null,
  refreshCsrfToken: () => Promise.resolve(null),
  onUnauthorized: () => undefined,
  requestReauth: () => Promise.resolve(false),
};

let hooks: ClientHooks = noHooks;

export function setClientHooks(next: Partial<ClientHooks>): void {
  hooks = { ...hooks, ...next };
}

export function resetClientHooks(): void {
  hooks = noHooks;
}

type FetchImpl = (request: Request) => Promise<Response>;

let fetchImpl: FetchImpl = (request) => globalThis.fetch(request);

/** Used by the tests to serve the contract from handlers instead of the network. */
export function setFetchImpl(impl: FetchImpl): void {
  fetchImpl = impl;
}

export function resetFetchImpl(): void {
  fetchImpl = (request) => globalThis.fetch(request);
}

function baseUrl(): string {
  return globalThis.location.origin;
}

async function problemOf(response: Response): Promise<unknown> {
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.includes("json")) return null;
  try {
    return (await response.clone().json()) as unknown;
  } catch {
    return null;
  }
}

async function codeOf(response: Response): Promise<string | null> {
  const body = await problemOf(response);
  if (body !== null && typeof body === "object" && "code" in body) {
    const { code } = body as { code?: unknown };
    return typeof code === "string" ? code : null;
  }
  return null;
}

function withCsrf(request: Request, token: string | null): Request {
  if (!UNSAFE_METHODS.has(request.method.toUpperCase()) || token === null) return request;
  const headers = new Headers(request.headers);
  headers.set("X-CSRF-Token", token);
  return new Request(request, { headers });
}

/**
 * Same-origin fetch for the console:
 * - sends the session cookie (`credentials: "same-origin"`) and never an `Authorization` header;
 * - adds `X-CSRF-Token` from memory on unsafe methods;
 * - re-reads the session once on `403 csrf_failed`, then replays the request;
 * - opens the re-authentication dialog once on `403 reauth_required`, then replays;
 * - tells the guard about `401`.
 */
export async function consoleFetch(request: Request): Promise<Response> {
  if (new URL(request.url).origin !== baseUrl()) {
    throw new ApiError(0, "unknown", null);
  }

  const send = async (attempt: Request, token: string | null): Promise<Response> => {
    const prepared = new Request(withCsrf(attempt, token), { credentials: "same-origin" });
    try {
      return await fetchImpl(prepared);
    } catch (error) {
      // A caller that cancelled — an unmounted page, a query whose key moved on —
      // asked for this. Turning it into a "the server could not be reached" would
      // put an error on screen for something nobody did wrong.
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "network", null);
    }
  };

  const replayable = request.clone();
  let response = await send(request, hooks.getCsrfToken());

  if (response.status === 403) {
    const code = await codeOf(response);
    if (code === "csrf_failed") {
      const token = await hooks.refreshCsrfToken();
      if (token !== null) {
        response = await send(replayable, token);
      }
    } else if (code === "reauth_required" && (await hooks.requestReauth())) {
      response = await send(replayable, hooks.getCsrfToken());
    }
  }

  // The replayed answer goes through this too: a session that ended between the first
  // call and the replay must send the user back to sign-in, not look like a plain
  // failure the caller has to interpret.
  if (response.status === 401) {
    hooks.onUnauthorized();
  }

  return response;
}

export const api = createClient<paths>({
  // Absolute, so the request is built the same way in the browser and in tests,
  // and so `consoleFetch` can refuse anything that is not same-origin.
  baseUrl: baseUrl(),
  credentials: "same-origin",
  cache: "no-store",
  // The API never redirects; following one could leak the request elsewhere.
  redirect: "error",
  fetch: (request) => consoleFetch(request),
});

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Returns the body of a successful call, or throws a typed `ApiError`. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await call;
  if (response.ok) return data as T;
  throw toApiError(response.status, error);
}

/** Same, but also gives the status, for the `202` polling answers. */
export async function unwrapWithStatus<T>(
  call: Promise<Result<T>>,
): Promise<{ data: T; status: number }> {
  const { data, error, response } = await call;
  if (response.ok) return { data: data as T, status: response.status };
  throw toApiError(response.status, error);
}
