import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function sessionsApi(sessions = [fixtures.session()]): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/me/sessions", () => ok({ sessions }));
}

describe("my sessions", () => {
  it("lists the devices and marks the current browser", async () => {
    const api = sessionsApi([
      fixtures.session(),
      fixtures.session({ id: "s2", kind: "web", device_name: "Firefox", current: true }),
    ]);

    renderApp({ api, route: "/sessions" });

    expect(await screen.findByText("Pixel 9")).toBeInTheDocument();
    expect(screen.getByText("This browser")).toBeInTheDocument();
  });

  it("asks before revoking a session, then revokes it", async () => {
    const user = userEvent.setup();
    const api = sessionsApi().on("DELETE", "/api/v1/me/sessions/{session_id}", (call) => {
      expect(call.path).toBe("/api/v1/me/sessions/s1");
      expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
      return noContent();
    });

    renderApp({ api, route: "/sessions" });

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(await screen.findByText("Sign Pixel 9 out?")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/me/sessions/{session_id}")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/me/sessions/{session_id}")).toHaveLength(1);
    });
  });

  it("warns that revoking the current session signs the user out", async () => {
    const user = userEvent.setup();
    const api = sessionsApi([
      fixtures.session({ id: "s2", kind: "web", device_name: "Firefox", current: true }),
    ]);

    renderApp({ api, route: "/sessions" });

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(
      await screen.findByText("This is the browser you are using: you will be signed out."),
    ).toBeInTheDocument();
  });
});

describe("deleting my data", () => {
  it("needs the checkbox and a confirmation, then signs the user out", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const api = sessionsApi()
      .on("DELETE", "/api/v1/me", (call) => {
        expect(call.path).toBe("/api/v1/me");
        deleted = true;
        return noContent();
      })
      .on("POST", "/api/v1/auth/logout", () => noContent())
      .replace("GET", "/api/v1/auth/web/session", () =>
        deleted ? problem(401, "unauthorized") : ok(fixtures.webSession()),
      );

    renderApp({ api, route: "/sessions" });

    const submit = await screen.findByRole("button", { name: "Delete my data" });
    expect(submit).toBeDisabled();

    await user.click(screen.getByLabelText("I understand this cannot be undone."));
    await user.click(submit);

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete my data" }));

    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/me")).toHaveLength(1);
    });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("refuses to delete the last administrator's data", async () => {
    const user = userEvent.setup();
    const api = sessionsApi().on("DELETE", "/api/v1/me", () => problem(409, "last_admin"));

    renderApp({ api, route: "/sessions" });

    await user.click(await screen.findByLabelText("I understand this cannot be undone."));
    await user.click(screen.getByRole("button", { name: "Delete my data" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete my data" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "There has to be at least one enabled administrator.",
    );
  });
});
