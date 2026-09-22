import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { MockApi, ok, problem } from "../../test/mock-api";
import { renderApp } from "../../test/render";

function apiWith(session: unknown, info = fixtures.serverInfo()): MockApi {
  const mock = new MockApi().on("GET", "/api/v1/server/info", () => ok(info));
  return mock.on("GET", "/api/v1/auth/web/session", () =>
    session === null ? problem(401, "unauthorized") : ok(session),
  );
}

describe("the console guard", () => {
  it("shows the setup wizard when there is no session and the server needs setup", async () => {
    renderApp({
      api: apiWith(null, fixtures.serverInfo({ setup_required: true, auth_methods: [] })),
      route: "/settings",
    });

    expect(await screen.findByLabelText("Setup code")).toBeInTheDocument();
  });

  it("sends an anonymous visitor of a set-up server to sign-in", async () => {
    renderApp({ api: apiWith(null), route: "/users" });

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("User name")).toBeInTheDocument();
  });

  it("sends a setup session to the wizard, whatever page was asked for", async () => {
    const api = apiWith(fixtures.setupSession(), fixtures.serverInfo({ setup_required: true }));
    api.on("GET", "/api/v1/setup/state", () => ok(fixtures.setupState()));

    renderApp({ api, route: "/connect-phone" });

    expect(await screen.findByRole("heading", { name: "Connect your media server" })).toBeInTheDocument();
  });

  it("opens the settings for an administrator", async () => {
    const api = apiWith(fixtures.webSession());
    api.on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/" });

    expect(await screen.findByRole("heading", { name: "Server settings" })).toBeInTheDocument();
    expect(screen.getByText("Signed in as Ada")).toBeInTheDocument();
  });

  it("keeps a non-admin out of the admin pages", async () => {
    const api = apiWith(
      fixtures.webSession({
        user: { id: "u2", name: "Bob", role: "user", media_server_admin: false },
      }),
    );
    api.on("POST", "/api/v1/pairings", () => ok(fixtures.newPairing(), 201));

    renderApp({ api, route: "/users" });

    expect(await screen.findByRole("heading", { name: "Connect a phone" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });

  it("signs the user out when the server refuses the session mid-session", async () => {
    const api = apiWith(fixtures.webSession());
    api.on("GET", "/api/v1/admin/settings", () => problem(401, "unauthorized"));

    renderApp({ api, route: "/settings" });

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    });
  });
});
