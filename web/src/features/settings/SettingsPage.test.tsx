import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function signedIn(user = fixtures.webSession()): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(user));
}

describe("the server settings", () => {
  it("sends only the fields that changed", async () => {
    const user = userEvent.setup();
    const api = signedIn()
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()))
      .on("PATCH", "/api/v1/admin/settings", (call) => {
        expect(call.body).toEqual({ name: "Salon" });
        expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
        return ok(fixtures.settings({ name: "Salon" }));
      });

    renderApp({ api, route: "/settings" });

    const name = await screen.findByLabelText("Server name");
    await user.clear(name);
    await user.type(name, "Salon");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
  });

  it("locks the fields the environment sets", async () => {
    const api = signedIn().on("GET", "/api/v1/admin/settings", () =>
      ok(fixtures.settings({ locked_fields: ["name", "public_url"] })),
    );

    renderApp({ api, route: "/settings" });

    expect(await screen.findByLabelText("Server name")).toBeDisabled();
    expect(screen.getByLabelText("Public address")).toBeDisabled();
    expect(screen.getAllByText(/Set by an environment variable\./).length).toBeGreaterThan(0);
  });

  it("keeps public_url and password sign-in read-only for a promoted admin", async () => {
    const api = signedIn(
      fixtures.webSession({
        user: { id: "u3", name: "Cyd", role: "admin", media_server_admin: false },
      }),
    ).on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/settings" });

    expect(await screen.findByLabelText("Public address")).toBeDisabled();
    expect(screen.getByLabelText("Password sign-in")).toBeDisabled();
    expect(
      screen.getAllByText("Only an administrator of the media server can change this."),
    ).not.toHaveLength(0);
  });

  it("explains a refused public address with the server's coarse reason", async () => {
    const user = userEvent.setup();
    const api = signedIn()
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings({ public_url: null })))
      .on("PATCH", "/api/v1/admin/settings", () =>
        problem(409, "public_url_unverified", { reason: "proof_mismatch" }),
      );

    renderApp({ api, route: "/settings" });

    await user.type(await screen.findByLabelText("Public address"), "https://wrong.example");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("another server answered");
  });

  it("asks for a re-authentication when the server demands one, then saves", async () => {
    const user = userEvent.setup();
    let fresh = false;
    const api = signedIn()
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings({ public_url: null })))
      .on("PATCH", "/api/v1/admin/settings", () =>
        fresh
          ? ok(fixtures.settings({ public_url: "https://tindarr.example.com" }))
          : problem(403, "reauth_required"),
      )
      .on("POST", "/api/v1/auth/web/reauth", (call) => {
        expect(call.body).toEqual({ password: "secret" });
        fresh = true;
        return ok({ reauth_expires_at: "2126-01-01T00:00:00Z" });
      });

    renderApp({ api, route: "/settings" });

    await user.type(await screen.findByLabelText("Public address"), "https://tindarr.example.com");
    await user.click(screen.getByRole("button", { name: "Save" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName("Confirm it is you");
    await user.type(screen.getByLabelText("Password for Ada"), "secret");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    // The wrapper replayed the refused PATCH once the re-authentication succeeded.
    await waitFor(() => {
      expect(screen.getByText("Settings saved.")).toBeInTheDocument();
    });
    expect(api.callsTo("PATCH", "/api/v1/admin/settings")).toHaveLength(2);
  });

  it("says why an unverified public address was refused", async () => {
    const user = userEvent.setup();
    const api = signedIn()
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings({ public_url: null })))
      .on("PATCH", "/api/v1/admin/settings", (call) => {
        expect(call.body).toEqual({ public_url: "https://elsewhere.example" });
        return problem(409, "public_url_unverified", { reason: "redirected" });
      });

    renderApp({ api, route: "/settings" });

    const field = await screen.findByLabelText("Public address");
    await user.type(field, "https://elsewhere.example");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That address did not answer as this server (redirected elsewhere).",
    );
  });

  it("asks a promoted admin to leave the media server settings alone", async () => {
    const api = signedIn(
      fixtures.webSession({
        user: { id: "u9", name: "Bo", role: "admin", media_server_admin: false },
      }),
    ).on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/settings" });

    expect(await screen.findByLabelText("Public address")).toBeDisabled();
    expect(screen.getByLabelText("Password sign-in")).toBeDisabled();
  });
});
