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
    locked_values: { server_type: null, url: null, verify_tls: null },
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

export function usageDay(overrides: Partial<Schemas["UsageDay"]> = {}): Schemas["UsageDay"] {
  return {
    date: "2026-09-24",
    user_id: "u2",
    generations: 3,
    input_tokens: 1200,
    output_tokens: 340,
    failures: 0,
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
    retry_after_ms: 2000,
    code: "q7Xc0vW2dYk9LmN4pRs6Tu",
    link: `tindarr://pair?server=${encodeURIComponent("https://tindarr.example.com")}&code=q7Xc0vW2dYk9LmN4pRs6Tu`,
    ...overrides,
  };
}

export function pairing(overrides: Partial<Schemas["Pairing"]> = {}): Schemas["Pairing"] {
  const status = overrides.status ?? "pending";
  return {
    id: "pair-1",
    status,
    created_at: "2026-09-22T10:00:00Z",
    expires_at: "2126-09-22T10:05:00Z",
    // As the server does: null once nothing more can happen, so the console stops.
    retry_after_ms: ["completed", "expired", "revoked"].includes(status) ? null : 2000,
    ...overrides,
  };
}

export function connector(
  kind: Schemas["ConnectorKind"],
  overrides: Partial<Schemas["Connector"]> = {},
): Schemas["Connector"] {
  return {
    kind,
    configured: false,
    secret: { set: false },
    locked_fields: [],
    status: { health: "not_configured" },
    ...overrides,
  };
}

/** Every kind, as `GET /admin/connectors` returns them, with nothing configured. */
export function connectors(
  overrides: Partial<Record<Schemas["ConnectorKind"], Partial<Schemas["Connector"]>>> = {},
): Schemas["Connector"][] {
  const kinds: Schemas["ConnectorKind"][] = ["media_server", "requests", "tmdb", "omdb", "llm"];
  return kinds.map((kind) => connector(kind, overrides[kind] ?? {}));
}

// --- the deck (roadmap 4.6) ---------------------------------------------------------

export function swipeStatus(
  overrides: Partial<Schemas["SwipeStatus"]> = {},
): Schemas["SwipeStatus"] {
  return {
    llm_configured: true,
    llm_provider: "ChatMock",
    tmdb_configured: true,
    requests_enabled: true,
    media_history: true,
    streaming_region: "FR",
    ratings_enabled: true,
    votes: 12,
    calibration: { done: 12, target: 20, complete: false },
    profile_ready: true,
    generations_left_today: 5,
    ...overrides,
  };
}

export function card(overrides: Partial<Schemas["Card"]> = {}): Schemas["Card"] {
  return {
    id: "card-1",
    media_type: "movie",
    tmdb_id: 27205,
    title: "Inception",
    original_title: "Inception",
    year: 2010,
    overview: "A thief who steals corporate secrets.",
    genres: ["Science-Fiction", "Thriller"],
    runtime_minutes: 148,
    seasons: null,
    poster_path: "/edv5CZvWj09upOsy2Y6IwDhK8bt.jpg",
    backdrop_path: null,
    ratings: { tmdb: 8.4, imdb: 8.8, rotten_tomatoes: 87, metacritic: 74 },
    providers: [
      { provider_id: 8, name: "Netflix", logo_path: null, offer: "subscription", subscribed: true },
      { provider_id: 2, name: "Apple TV", logo_path: null, offer: "rent", subscribed: false },
    ],
    trailer: { site: "youtube", key: "YoHD9XEInc0", name: "Trailer", language: "en" },
    rationale: "Heists inside dreams, which is your kind of puzzle.",
    pick_type: "safe",
    availability: "none",
    expires_at: "2126-09-25T10:00:00Z",
    ...overrides,
  };
}

export function deck(overrides: Partial<Schemas["Deck"]> = {}): Schemas["Deck"] {
  return {
    mode: "normal",
    novelty: "balanced",
    cards: [card()],
    calibration: { done: 12, target: 20, complete: false },
    ...overrides,
  };
}

export function preferences(
  overrides: Partial<Schemas["Preferences"]> = {},
): Schemas["Preferences"] {
  return {
    media_type: "both",
    novelty: "balanced",
    auto_request: false,
    language: "fr-FR",
    streaming_services: [8],
    ...overrides,
  };
}

export function stats(overrides: Partial<Schemas["Stats"]> = {}): Schemas["Stats"] {
  return {
    total: 10,
    likes: 4,
    dislikes: 3,
    seen_liked: 2,
    seen_disliked: 1,
    skips: 2,
    requested: 1,
    like_rate: 0.4,
    request_rate: 0.1,
    by_pick_type: { safe: { total: 6, likes: 3 }, explore: { total: 4, likes: 1 } },
    ...overrides,
  };
}

export function profileState(
  overrides: Partial<Schemas["ProfileState"]> = {},
): Schemas["ProfileState"] {
  return {
    profile: {
      text: "Loves: heists. Avoids: musicals.",
      user_edited: false,
      updated_at: "2026-09-23T20:00:00Z",
      votes_since_update: 3,
    },
    refreshing: false,
    refresh_error: null,
    ...overrides,
  };
}

export function like(overrides: Partial<Schemas["Like"]> = {}): Schemas["Like"] {
  return {
    media_type: "movie",
    tmdb_id: 27205,
    title: "Inception",
    year: 2010,
    poster_path: null,
    pick_type: "safe",
    liked_at: "2026-09-23T20:00:00Z",
    requested: false,
    availability: "none",
    watch_url: null,
    ...overrides,
  };
}
