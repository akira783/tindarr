import { describe, expect, it, vi } from "vitest";

import { MockApi, noContent, ok, problem } from "../../test/mock-api";
import { api, consoleFetch, setClientHooks, setFetchImpl, unwrap } from "./client";
import { ApiError } from "./problem";
import type { components } from "./schema";

type WebSession = components["schemas"]["WebSession"];

const session: WebSession = {
  kind: "web",
  csrf_token: "csrf-1",
  expires_at: "2026-10-01T00:00:00Z",
  user: { id: "u1", name: "Ada", role: "admin" },
};

function install(mock: MockApi): void {
  setFetchImpl(mock.fetch);
}

describe("the console fetch wrapper", () => {
  it("adds the CSRF token on unsafe methods and sends the cookie", async () => {
    const mock = new MockApi()
      .on("POST", "/api/v1/pairings", () => ok({ id: "p1" }, 201))
      .on("GET", "/api/v1/admin/users", () => ok({ users: [] }));
    install(mock);
    setClientHooks({ getCsrfToken: () => "csrf-1" });

    await api.POST("/api/v1/pairings");
    await api.GET("/api/v1/admin/users");

    const post = mock.calls[0];
    const get = mock.calls[1];
    expect(post?.headers.get("X-CSRF-Token")).toBe("csrf-1");
    expect(post?.credentials).toBe("same-origin");
    expect(get?.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("never sends an Authorization header", async () => {
    const mock = new MockApi().on("GET", "/api/v1/me", () => ok({ id: "u1" }));
    install(mock);

    await api.GET("/api/v1/me");

    expect(mock.calls[0]?.headers.get("Authorization")).toBeNull();
  });

  it("re-reads the session once on csrf_failed and replays the request", async () => {
    let first = true;
    const mock = new MockApi().on("DELETE", "/api/v1/me/sessions/{session_id}", (call) => {
      if (first) {
        first = false;
        return problem(403, "csrf_failed");
      }
      return call.headers.get("X-CSRF-Token") === "csrf-2" ? noContent() : problem(403, "csrf_failed");
    });
    install(mock);

    let token = "csrf-1";
    const refreshCsrfToken = vi.fn(() => {
      token = "csrf-2";
      return Promise.resolve(token);
    });
    setClientHooks({ getCsrfToken: () => token, refreshCsrfToken });

    const { response } = await api.DELETE("/api/v1/me/sessions/{session_id}", {
      params: { path: { session_id: "s1" } },
    });

    expect(refreshCsrfToken).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(204);
    expect(mock.calls).toHaveLength(2);
  });

  it("asks for a re-authentication on reauth_required, then replays once", async () => {
    let refused = true;
    const mock = new MockApi().on("PATCH", "/api/v1/admin/settings", () => {
      if (refused) {
        refused = false;
        return problem(403, "reauth_required");
      }
      return ok({ name: "Tindeerr" });
    });
    install(mock);

    const requestReauth = vi.fn(() => {
      return Promise.resolve(true);
    });
    setClientHooks({ getCsrfToken: () => "csrf-1", requestReauth });

    const { response } = await api.PATCH("/api/v1/admin/settings", {
      body: { name: "Tindeerr" },
    });

    expect(requestReauth).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(200);
    // The replayed request carries the same body.
    expect(mock.calls).toHaveLength(2);
    expect(mock.calls[1]?.body).toEqual({ name: "Tindeerr" });
  });

  it("does not replay when the user cancels the re-authentication", async () => {
    const mock = new MockApi().on("PATCH", "/api/v1/admin/settings", () =>
      problem(403, "reauth_required"),
    );
    install(mock);
    setClientHooks({ requestReauth: () => Promise.resolve(false) });

    const { response } = await api.PATCH("/api/v1/admin/settings", { body: { name: "x" } });

    expect(response.status).toBe(403);
    expect(mock.calls).toHaveLength(1);
  });

  it("tells the guard about a 401", async () => {
    const mock = new MockApi().on("GET", "/api/v1/auth/web/session", () =>
      problem(401, "unauthorized"),
    );
    install(mock);
    const onUnauthorized = vi.fn();
    setClientHooks({ onUnauthorized });

    await api.GET("/api/v1/auth/web/session");

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("refuses a request that is not same-origin", async () => {
    install(new MockApi());
    await expect(consoleFetch(new Request("https://elsewhere.example/api/v1/me"))).rejects.toThrow(
      ApiError,
    );
  });

  it("turns a transport failure into a network error", async () => {
    setFetchImpl(() => Promise.reject(new TypeError("offline")));
    await expect(
      consoleFetch(new Request(`${globalThis.location.origin}/api/v1/me`)),
    ).rejects.toMatchObject({ code: "network" });
  });

  it("unwraps a problem body into a typed error", async () => {
    const mock = new MockApi().on("GET", "/api/v1/auth/web/session", () => ok(session));
    install(mock);
    await expect(unwrap(api.GET("/api/v1/auth/web/session"))).resolves.toEqual(session);

    mock.replace("GET", "/api/v1/auth/web/session", () =>
      problem(429, "rate_limited", { retry_after_ms: 4000 }),
    );
    await expect(unwrap(api.GET("/api/v1/auth/web/session"))).rejects.toMatchObject({
      code: "rate_limited",
      status: 429,
    });
  });
});
