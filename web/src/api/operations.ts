import { api, unwrap, unwrapWithStatus } from "./client";
import { isApiError } from "./problem";
import type { components } from "./schema";

type Schemas = components["schemas"];

export type ServerInfo = Schemas["ServerInfo"];
export type AuthMethod = Schemas["AuthMethod"];
export type SetupState = Schemas["SetupState"];
export type WebSession = Schemas["WebSession"];
export type User = Schemas["User"];
export type AdminUser = Schemas["AdminUser"];
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
