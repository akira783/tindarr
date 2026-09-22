import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function pendingSetup(): MockApi {
  return new MockApi().on("GET", "/api/v1/server/info", () =>
    ok(fixtures.serverInfo({ setup_required: true, auth_methods: [], media_server: null })),
  );
}

describe("the setup wizard", () => {
  it("claims the server with the setup code, then asks for the media server", async () => {
    const user = userEvent.setup();
    let claimed = false;
    const api = pendingSetup()
      .on("GET", "/api/v1/auth/web/session", () =>
        claimed ? ok(fixtures.setupSession()) : problem(401, "unauthorized"),
      )
      .on("POST", "/api/v1/setup/claim", (call) => {
        if ((call.body as { setup_code?: string }).setup_code !== "ABCDEFGH1234") {
          return problem(401, "invalid_setup_code");
        }
        claimed = true;
        return ok(fixtures.setupSession());
      })
      .on("GET", "/api/v1/setup/state", () => ok(fixtures.setupState()));

    renderApp({ api, route: "/setup" });

    const field = await screen.findByLabelText("Setup code");
    await user.type(field, "WRONGCODE123");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That setup code is not correct.");

    await user.clear(field);
    await user.type(field, "ABCDEFGH1234");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      await screen.findByRole("heading", { name: "Connect your media server" }),
    ).toBeInTheDocument();
  });

  it("saves the media server and moves on to the administrator sign-in", async () => {
    const user = userEvent.setup();
    let configured = false;
    const api = pendingSetup()
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.setupSession()))
      .on("GET", "/api/v1/setup/state", () =>
        ok(
          configured
            ? fixtures.setupState({
                media_server: { kind: "jellyfin", name: "Jellyfin" },
                auth_methods: ["password", "quick_connect"],
              })
            : fixtures.setupState(),
        ),
      )
      .on("PUT", "/api/v1/setup/media-server", (call) => {
        const body = call.body as { url?: string; api_key?: string; verify_tls?: boolean };
        expect(body.url).toBe("http://192.168.1.20:8096");
        expect(body.api_key).toBe("the-key");
        expect(call.headers.get("X-CSRF-Token")).toBe("setup-csrf");
        configured = true;
        return ok({ health: "ok", server_name: "Jellyfin" });
      });

    renderApp({ api, route: "/setup" });

    await user.type(await screen.findByLabelText("Address"), "http://192.168.1.20:8096");
    await user.type(screen.getByLabelText("Administrator API key"), "the-key");
    await user.click(screen.getByRole("button", { name: "Test and save" }));

    expect(
      await screen.findByRole("heading", { name: "Sign in as an administrator" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Quick Connect" })).toBeInTheDocument();
  });

  it("skips the media server step when the environment locks it", async () => {
    const api = pendingSetup()
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.setupSession()))
      .on("GET", "/api/v1/setup/state", () =>
        ok(
          fixtures.setupState({
            media_server_locked: true,
            locked_fields: ["server_type", "url", "api_key"],
            auth_methods: ["password"],
          }),
        ),
      );

    renderApp({ api, route: "/setup" });

    expect(
      await screen.findByText(
        "The media server is set by environment variables on this server, so this step is skipped.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("User name")).toBeInTheDocument();
  });

  it("ends on the public address once the administrator sign-in completed setup", async () => {
    const user = userEvent.setup();
    let completed = false;
    const api = pendingSetup()
      .on("GET", "/api/v1/auth/web/session", () =>
        ok(completed ? fixtures.webSession() : fixtures.setupSession()),
      )
      .on("GET", "/api/v1/setup/state", () =>
        ok(
          fixtures.setupState({
            media_server: { kind: "jellyfin", name: "Jellyfin" },
            auth_methods: ["password"],
          }),
        ),
      )
      .on("POST", "/api/v1/auth/web/login", () => {
        completed = true;
        return ok(fixtures.webSession({ setup_completed_now: true }));
      })
      .on("PATCH", "/api/v1/admin/settings", (call) => {
        expect((call.body as { public_url?: string }).public_url).toBe(globalThis.location.origin);
        return ok(fixtures.settings({ public_url: globalThis.location.origin }));
      })
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/setup" });

    await user.type(await screen.findByLabelText("User name"), "ada");
    await user.type(screen.getByLabelText("Password"), "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Public address" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));

    // Saving the public address finishes the wizard and opens the console.
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Server settings" })).toBeInTheDocument();
    });
  });
});
