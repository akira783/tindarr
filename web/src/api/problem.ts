import type { components } from "./schema";

export type Problem = components["schemas"]["Problem"];

/**
 * Every `code` the contract documents (api/openapi.yaml, `Problem.code`).
 * `src/api/problem.test.ts` fails when this list and the contract drift apart,
 * and the catalogs must carry a message for each (see `src/i18n/errors.ts`).
 */
export const PROBLEM_CODES = [
  // request and credentials
  "validation_error",
  "bad_request",
  "host_not_allowed",
  "unauthorized",
  "token_expired",
  "refresh_token_reused",
  "csrf_failed",
  "https_required",
  "forbidden",
  "admin_required",
  "media_server_admin_required",
  "reauth_required",
  "remote_access_denied",
  "not_found",
  "method_not_allowed",
  "rate_limited",
  "internal_error",
  // setup
  "setup_required",
  "setup_completed",
  "invalid_setup_code",
  "setup_session_required",
  "setting_locked",
  // sign-in
  "invalid_credentials",
  "account_disabled",
  "not_a_server_user",
  "password_sign_in_disabled",
  "sign_in_method_unavailable",
  "pin_expired",
  "plex_pin_pending",
  "plex_owner_required",
  "plex_tv_unreachable",
  "quick_connect_unavailable",
  "quick_connect_expired",
  "media_server_unreachable",
  "media_server_changed",
  // pairing
  "pairing_expired",
  "pairing_rejected",
  "pairing_not_awaiting_approval",
  "public_url_not_set",
  "public_url_unverified",
  // connectors
  "connector_unreachable",
  "connector_unauthorized",
  "connector_unexpected_response",
  "media_server_unsupported",
  "media_server_required",
  "secret_required",
  // swipe and admin
  "last_admin",
  "streaming_region_not_set",
  "metadata_unreachable",
  "llm_not_configured",
  "tmdb_not_configured",
  "llm_auth_failed",
  "llm_quota",
  "llm_model_not_found",
  "llm_unreachable",
  "llm_invalid_output",
  "daily_limit_reached",
  "requests_not_configured",
  "no_backend_user",
  "title_not_offered",
  "request_not_allowed",
  "quota_exceeded",
  "request_backend_error",
  // imports and the calibration grid
  "import_unreadable",
  "import_too_large",
  "import_in_progress",
] as const;

export type ProblemCode = (typeof PROBLEM_CODES)[number];

/** Codes the console produces itself, for a failure that never reached the server. */
export type LocalErrorCode = "network" | "unknown";

export type ErrorCode = ProblemCode | LocalErrorCode;

const KNOWN = new Set<string>(PROBLEM_CODES);

export function isProblemCode(value: unknown): value is ProblemCode {
  return typeof value === "string" && KNOWN.has(value);
}

/** A problem detail the console understands, or a transport failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode;
  readonly problem: Problem | null;

  constructor(status: number, code: ErrorCode, problem: Problem | null) {
    super(`${code} (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.problem = problem;
  }

  get retryAfterMs(): number | null {
    return this.problem?.retry_after_ms ?? null;
  }

  /** Coarse cause of `public_url_unverified`. */
  get reason(): string | null {
    return this.problem?.reason ?? null;
  }

  get fieldErrors(): { field: string; message: string }[] {
    return this.problem?.errors ?? [];
  }

  is(...codes: ErrorCode[]): boolean {
    return codes.includes(this.code);
  }
}

/** Turns an unparsed error body into a typed problem. Never throws. */
export function toApiError(status: number, body: unknown): ApiError {
  if (body !== null && typeof body === "object" && "code" in body) {
    const problem = body as Problem;
    return new ApiError(status, isProblemCode(problem.code) ? problem.code : "unknown", problem);
  }
  return new ApiError(status, "unknown", null);
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}
