import { api, consoleFetch, unwrap, unwrapWithStatus } from "./client";
import { isApiError, toApiError } from "./problem";
import type { components } from "./schema";

type Schemas = components["schemas"];

export type ServerInfo = Schemas["ServerInfo"];
export type AuthMethod = Schemas["AuthMethod"];
export type SetupState = Schemas["SetupState"];
export type WebSession = Schemas["WebSession"];
export type User = Schemas["User"];
export type AdminUser = Schemas["AdminUser"];
export type UsageDay = Schemas["UsageDay"];
export type Session = Schemas["Session"];
export type Pairing = Schemas["Pairing"];
export type NewPairing = Schemas["NewPairing"];
export type ServerSettings = Schemas["ServerSettings"];
export type ServerSettingsPatch = Schemas["ServerSettingsPatch"];
export type PasswordSignIn = Schemas["PasswordSignIn"];
export type MediaServerKind = Schemas["MediaServerKind"];
export type MediaServerConfigInput = Schemas["MediaServerConfigInput"];
export type ConnectorStatus = Schemas["ConnectorStatus"];
export type ConnectorHealth = Schemas["ConnectorHealth"];
export type Connector = Schemas["Connector"];
export type ConnectorKind = Schemas["ConnectorKind"];
export type ConnectorInput = Schemas["ConnectorInput"];
export type ApiKeyInput = Schemas["ApiKeyInput"];
export type RequestsInput = Schemas["RequestsInput"];
export type LlmSettingsInput = Schemas["LlmSettingsInput"];
export type LlmProviderKind = Schemas["LlmProviderKind"];
export type ReauthInput = Schemas["ReauthInput"];
export type Role = Schemas["Role"];

/** A `202` answer of a sign-in or re-authentication that is still waiting. */
export interface Waiting {
  waiting: true;
  retryAfterMs: number | null;
}

function waiting(status: number, body: unknown): Waiting | null {
  if (status !== 202) return null;
  const retry =
    body !== null && typeof body === "object" && "retry_after_ms" in body
      ? ((body as { retry_after_ms?: number }).retry_after_ms ?? null)
      : null;
  return { waiting: true, retryAfterMs: retry };
}

// ------------------------------------------------------------------ server

export function getServerInfo(): Promise<ServerInfo> {
  return unwrap(api.GET("/api/v1/server/info"));
}

// ------------------------------------------------------------------ session

/** The current web or setup session, or null when there is none (`401`). */
export async function getWebSession(): Promise<WebSession | null> {
  try {
    return await unwrap(api.GET("/api/v1/auth/web/session"));
  } catch (error) {
    if (isApiError(error) && error.status === 401) return null;
    throw error;
  }
}

export function logout(): Promise<void> {
  return unwrap(api.POST("/api/v1/auth/logout")).then(() => undefined);
}

// ------------------------------------------------------------------ setup

export function claimSetup(setupCode: string): Promise<WebSession> {
  return unwrap(api.POST("/api/v1/setup/claim", { body: { setup_code: setupCode } }));
}

export function getSetupState(): Promise<SetupState> {
  return unwrap(api.GET("/api/v1/setup/state"));
}

export function saveSetupMediaServer(body: MediaServerConfigInput): Promise<ConnectorStatus> {
  return unwrap(api.PUT("/api/v1/setup/media-server", { body }));
}

// ------------------------------------------------------------------ sign-in

export function signInWithPassword(username: string, password: string): Promise<WebSession> {
  return unwrap(api.POST("/api/v1/auth/web/login", { body: { username, password } }));
}

export function createPlexPin(
  purpose: "sign_in" | "reauth" | "owner_token",
): Promise<{ pin_id: string; auth_url: string; expires_at: string }> {
  return unwrap(api.POST("/api/v1/auth/plex/pins", { body: { purpose } }));
}

export function getPlexPinStatus(
  pinId: string,
): Promise<{ status: "pending" | "authorized"; expires_at: string; account_name?: string | null }> {
  return unwrap(api.POST("/api/v1/auth/plex/pins/status", { body: { pin_id: pinId } }));
}

export async function signInWithPlexPin(pinId: string): Promise<WebSession | Waiting> {
  const { data, status } = await unwrapWithStatus(
    api.POST("/api/v1/auth/web/plex/login", { body: { pin_id: pinId } }),
  );
  return waiting(status, data) ?? (data as WebSession);
}

export function createQuickConnect(
  purpose: "sign_in" | "reauth",
): Promise<{ handle: string; code: string; expires_at: string }> {
  return unwrap(api.POST("/api/v1/auth/quick-connect", { body: { purpose } }));
}

export async function signInWithQuickConnect(handle: string): Promise<WebSession | Waiting> {
  const { data, status } = await unwrapWithStatus(
    api.POST("/api/v1/auth/web/quick-connect/login", { body: { handle } }),
  );
  return waiting(status, data) ?? (data as WebSession);
}

export async function reauthenticate(
  body: ReauthInput,
): Promise<{ reauth_expires_at: string } | Waiting> {
  const { data, status } = await unwrapWithStatus(api.POST("/api/v1/auth/web/reauth", { body }));
  return waiting(status, data) ?? (data as { reauth_expires_at: string });
}

export function isWaiting(value: unknown): value is Waiting {
  return value !== null && typeof value === "object" && "waiting" in value;
}

// ------------------------------------------------------------------ admin

export function getSettings(): Promise<ServerSettings> {
  return unwrap(api.GET("/api/v1/admin/settings"));
}

export function updateSettings(body: ServerSettingsPatch): Promise<ServerSettings> {
  return unwrap(api.PATCH("/api/v1/admin/settings", { body }));
}

export function listConnectors(): Promise<Connector[]> {
  return unwrap(api.GET("/api/v1/admin/connectors")).then((result) => result.connectors);
}

export function saveConnector(kind: ConnectorKind, body: ConnectorInput): Promise<Connector> {
  return unwrap(api.PUT("/api/v1/admin/connectors/{kind}", { params: { path: { kind } }, body }));
}

export function testConnector(
  kind: ConnectorKind,
  body: ConnectorInput,
): Promise<ConnectorStatus> {
  return unwrap(
    api.POST("/api/v1/admin/connectors/{kind}/test", { params: { path: { kind } }, body }),
  );
}

export function deleteConnector(kind: ConnectorKind): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/admin/connectors/{kind}", { params: { path: { kind } } }),
  ).then(() => undefined);
}

/** Model ids the provider itself offers, so none is ever hard-coded in the console. */
export function listLlmModels(body: LlmSettingsInput): Promise<string[]> {
  return unwrap(api.POST("/api/v1/admin/llm/models", { body })).then((result) => result.models);
}

export function listUsers(): Promise<AdminUser[]> {
  return unwrap(api.GET("/api/v1/admin/users")).then((result) => result.users);
}

export function getUsage(days: number): Promise<UsageDay[]> {
  return unwrap(api.GET("/api/v1/admin/usage", { params: { query: { days } } })).then(
    (result) => result.days,
  );
}

export function updateUser(
  userId: string,
  body: { enabled?: boolean; role?: Role; daily_generation_limit?: number | null },
): Promise<AdminUser> {
  return unwrap(api.PATCH("/api/v1/admin/users/{user_id}", { params: { path: { user_id: userId } }, body }));
}

export function revokeUserSessions(userId: string): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/admin/users/{user_id}/sessions", {
      params: { path: { user_id: userId } },
    }),
  ).then(() => undefined);
}

// ------------------------------------------------------------------ pairing

export function createPairing(): Promise<NewPairing> {
  return unwrap(api.POST("/api/v1/pairings"));
}

export function getPairing(pairingId: string): Promise<Pairing> {
  return unwrap(
    api.GET("/api/v1/pairings/{pairing_id}", { params: { path: { pairing_id: pairingId } } }),
  );
}

export function approvePairing(pairingId: string): Promise<Pairing> {
  return unwrap(
    api.POST("/api/v1/pairings/{pairing_id}/approve", {
      params: { path: { pairing_id: pairingId } },
    }),
  );
}

export function revokePairing(pairingId: string): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/pairings/{pairing_id}", { params: { path: { pairing_id: pairingId } } }),
  ).then(() => undefined);
}

// ------------------------------------------------------------------ me

export function listMySessions(): Promise<Session[]> {
  return unwrap(api.GET("/api/v1/me/sessions")).then((result) => result.sessions);
}

export function revokeMySession(sessionId: string): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/me/sessions/{session_id}", {
      params: { path: { session_id: sessionId } },
    }),
  ).then(() => undefined);
}

export function deleteMyData(): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/me", { params: { query: { confirm: "delete-my-data" } } }),
  ).then(() => undefined);
}

// --------------------------------------------- what the household already watched

export type Import = Schemas["Import"];
export type ImportFormat = Schemas["ImportFormat"];
export type ImportReviewEntry = Schemas["ImportReviewEntry"];
export type ImportCandidate = Schemas["ImportCandidate"];
export type GridTitle = Schemas["GridTitle"];
export type GridAnswer = Schemas["GridAnswer"];
export type TitleRef = Schemas["TitleRef"];

/**
 * Upload one export. The file is the body, not a form: `openapi-fetch` assumes JSON, so
 * this builds the request by hand and sends it through `consoleFetch`, which is what
 * adds the CSRF token, keeps it same-origin and handles `401`.
 *
 * The bytes are read first rather than the `File` being handed to `Request` directly.
 * An export is a megabyte, the server refuses anything past eight, and passing the
 * handle instead would save nothing that matters while making the one interesting path
 * here impossible to test — jsdom's `File` and the fetch implementation under it are
 * not the same object. The `Content-Type` is set explicitly for the same reason: the
 * server detects the format from the bytes, but a body with no type at all is a body
 * some proxy will decide things about.
 */
export async function startImport(file: File): Promise<Import> {
  const request = new Request(`${globalThis.location.origin}/api/v1/swipe/imports`, {
    method: "POST",
    body: await file.arrayBuffer(),
    headers: { "Content-Type": file.type === "" ? "application/octet-stream" : file.type },
  });
  const response = await consoleFetch(request);
  if (!response.ok) throw toApiError(response.status, await bodyOf(response));
  return (await response.json()) as Import;
}

async function bodyOf(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch {
    return null;
  }
}

export function listImports(): Promise<Import[]> {
  return unwrap(api.GET("/api/v1/swipe/imports")).then((result) => result.imports);
}

export function deleteImport(importId: string): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/swipe/imports/{import_id}", {
      params: { path: { import_id: importId } },
    }),
  ).then(() => undefined);
}

export function listImportReview(
  importId: string,
): Promise<{ entries: ImportReviewEntry[]; pending: number }> {
  return unwrap(
    api.GET("/api/v1/swipe/imports/{import_id}/review", {
      params: { path: { import_id: importId } },
    }),
  );
}

export function decideImportReview(
  importId: string,
  entryId: string,
  body: { decision: "accept" | "reject"; title?: TitleRef },
): Promise<void> {
  return unwrap(
    api.POST("/api/v1/swipe/imports/{import_id}/review/{entry_id}", {
      params: { path: { import_id: importId, entry_id: entryId } },
      body,
    }),
  ).then(() => undefined);
}

export function getCalibrationGrid(page: number): Promise<{ page: number; titles: GridTitle[] }> {
  return unwrap(api.GET("/api/v1/swipe/calibration/grid", { params: { query: { page } } }));
}

export function submitCalibrationGrid(answers: GridAnswer[]): Promise<number> {
  return unwrap(api.POST("/api/v1/swipe/calibration/grid", { body: { answers } })).then(
    (result) => result.recorded,
  );
}

// ------------------------------------------------------------------ the deck

export type SwipeStatus = Schemas["SwipeStatus"];
export type Deck = Schemas["Deck"];
export type Card = Schemas["Card"];
export type Calibration = Schemas["Calibration"];
export type MediaFilter = Schemas["MediaFilter"];
export type Novelty = Schemas["Novelty"];
export type VoteValue = Schemas["VoteValue"];
export type VoteInput = Schemas["VoteInput"];
export type VoteResult = Schemas["VoteResult"];
export type RequestStatus = Schemas["RequestStatus"];
export type Like = Schemas["Like"];
export type ProfileState = Schemas["ProfileState"];
export type Preferences = Schemas["Preferences"];
export type PreferencesPatch = Schemas["PreferencesPatch"];
export type Stats = Schemas["Stats"];
export type StreamingProvider = Schemas["StreamingProvider"];
export type Availability = Schemas["Availability"];
export type PickType = Schemas["PickType"];

export function getSwipeStatus(): Promise<SwipeStatus> {
  return unwrap(api.GET("/api/v1/swipe/status"));
}

export interface DeckQuery {
  media_type?: MediaFilter;
  novelty?: Novelty;
  mood?: string;
}

/**
 * The next cards, or `{ waiting: true }` while a batch is built.
 *
 * The `202` is not an error and must not be treated as one: the caller waits
 * `retryAfterMs` and asks again (`api/openapi.yaml`, `getDeck`).
 */
export async function getDeck(query: DeckQuery = {}): Promise<Deck | Waiting> {
  const { data, status } = await unwrapWithStatus(
    api.GET("/api/v1/swipe/deck", { params: { query } }),
  );
  return waiting(status, data) ?? (data as Deck);
}

export interface VoteOutcome {
  results: VoteResult[];
  profile_refresh_started?: boolean;
}

export function submitVotes(votes: VoteInput[]): Promise<VoteOutcome> {
  return unwrap(api.POST("/api/v1/swipe/votes", { body: { votes } }));
}

export function undoVote(mediaType: "movie" | "tv", tmdbId: number): Promise<void> {
  return unwrap(
    api.DELETE("/api/v1/swipe/votes/{media_type}/{tmdb_id}", {
      params: { path: { media_type: mediaType, tmdb_id: tmdbId } },
    }),
  ).then(() => undefined);
}

export function requestTitle(title: TitleRef): Promise<RequestStatus> {
  return unwrap(api.POST("/api/v1/swipe/requests", { body: title })).then(
    (result) => result.request_status,
  );
}

export function listLikes(
  status: "all" | "to_request" | "requested" = "all",
): Promise<{ likes: Like[]; next_cursor?: string | null }> {
  return unwrap(api.GET("/api/v1/swipe/likes", { params: { query: { status } } }));
}

export function getProfile(): Promise<ProfileState> {
  return unwrap(api.GET("/api/v1/swipe/profile"));
}

export function saveProfile(text: string): Promise<ProfileState> {
  return unwrap(api.PUT("/api/v1/swipe/profile", { body: { text } }));
}

export function refreshProfile(): Promise<boolean> {
  return unwrap(api.POST("/api/v1/swipe/profile/refresh")).then((result) => result.started);
}

export function getPreferences(): Promise<Preferences> {
  return unwrap(api.GET("/api/v1/swipe/preferences"));
}

export function updatePreferences(body: PreferencesPatch): Promise<Preferences> {
  return unwrap(api.PATCH("/api/v1/swipe/preferences", { body }));
}

export function getStats(): Promise<Stats> {
  return unwrap(api.GET("/api/v1/swipe/stats"));
}
