import type { components } from "../src/api/schema";

type Schemas = components["schemas"];

export function serverInfo(overrides: Partial<Schemas["ServerInfo"]> = {}): Schemas["ServerInfo"] {
  return {
    name: "Maison",
    version: "1.0.0",
    api_version: 1,
    min_app_version: "1.0.0",
    setup_required: false,
    media_server: { kind: "jellyfin", name: "Jellyfin" },
    auth_methods: ["password"],
    capabilities: [],
    tmdb_image_base_url: "https://image.tmdb.org/t/p/",
    ...overrides,
  };
}

export function webSession(
  overrides: Partial<Schemas["WebSession"]> = {},
): Schemas["WebSession"] {
  return {
    kind: "web",
    csrf_token: "csrf-token",
    expires_at: "2026-09-29T10:00:00Z",
    reauth_expires_at: "2026-09-22T10:05:00Z",
    user: { id: "u1", name: "Ada", role: "admin", media_server_admin: true },
    ...overrides,
  };
}

export function setupSession(
  overrides: Partial<Schemas["WebSession"]> = {},
): Schemas["WebSession"] {
  return {
    kind: "setup",
    csrf_token: "setup-csrf",
    expires_at: "2026-09-22T10:30:00Z",
    user: null,
    ...overrides,
  };
}

export function setupState(overrides: Partial<Schemas["SetupState"]> = {}): Schemas["SetupState"] {
  return {
    media_server: null,
    media_server_locked: false,
    locked_fields: [],
    auth_methods: [],
    ...overrides,
  };
}

export function adminUser(overrides: Partial<Schemas["AdminUser"]> = {}): Schemas["AdminUser"] {
  return {
    id: "u2",
    name: "Bob",
    role: "user",
    enabled: true,
    media_server_admin: false,
    promoted: false,
    remote_access: true,
    created_at: "2026-09-01T10:00:00Z",
    last_sign_in_at: "2026-09-20T18:00:00Z",
    votes: 0,
    generations_today: 0,
    ...overrides,
  };
}

export function settings(
  overrides: Partial<Schemas["ServerSettings"]> = {},
): Schemas["ServerSettings"] {
  return {
    name: "Maison",
    public_url: "https://tindarr.example.com",
    password_sign_in: "enabled",
    language: "fr-FR",
    streaming_region: "FR",
    daily_generation_limit: 20,
    warm_up_enabled: true,
    content_filters: { exclude_adult: true, min_year: null, excluded_genres: [] },
    locked_fields: [],
    ...overrides,
  };
}

export function session(overrides: Partial<Schemas["Session"]> = {}): Schemas["Session"] {
  return {
    id: "s1",
    kind: "mobile",
    device_name: "Pixel 9",
    platform: "android",
    created_at: "2026-09-20T10:00:00Z",
    last_seen_at: "2026-09-22T09:00:00Z",
    current: false,
    ...overrides,
  };
}

export function newPairing(overrides: Partial<Schemas["NewPairing"]> = {}): Schemas["NewPairing"] {
  return {
    id: "pair-1",
    status: "pending",
    created_at: "2026-09-22T10:00:00Z",
    expires_at: "2126-09-22T10:05:00Z",
    code: "q7Xc0vW2dYk9LmN4pRs6Tu",
    link: `tindarr://pair?server=${encodeURIComponent("https://tindarr.example.com")}&code=q7Xc0vW2dYk9LmN4pRs6Tu`,
    ...overrides,
  };
}

export function pairing(overrides: Partial<Schemas["Pairing"]> = {}): Schemas["Pairing"] {
  return {
    id: "pair-1",
    status: "pending",
    created_at: "2026-09-22T10:00:00Z",
    expires_at: "2126-09-22T10:05:00Z",
    ...overrides,
  };
}
