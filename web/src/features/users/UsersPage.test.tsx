import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function usersApi(users = [fixtures.adminUser()]): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/admin/users", () => ok({ users }));
}

describe("the users page", () => {
  it("lists users with their role, state and last sign-in", async () => {
    const api = usersApi([
      fixtures.adminUser({ name: "Bob" }),
      fixtures.adminUser({
        id: "u3",
        name: "Cyd",
        role: "admin",
        media_server_admin: true,
        enabled: false,
        disabled_reason: "media_server",
        remote_access: false,
      }),
    ]);

    renderApp({ api, route: "/users" });

    expect(await screen.findByRole("rowheader", { name: /Bob/ })).toBeInTheDocument();
    const cyd = screen.getByRole("rowheader", { name: /Cyd/ }).closest("tr");
    expect(cyd).not.toBeNull();
    expect(within(cyd!).getByText("Disabled on the media server")).toBeInTheDocument();
    expect(within(cyd!).getByText("Administrator on the media server")).toBeInTheDocument();
    expect(within(cyd!).getByText("No remote access")).toBeInTheDocument();
  });

  it("promotes a user", async () => {
    const user = userEvent.setup();
    const api = usersApi().on("PATCH", "/api/v1/admin/users/{user_id}", (call) => {
      expect(call.path).toBe("/api/v1/admin/users/u2");
      expect(call.body).toEqual({ role: "admin" });
      expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
      return ok(fixtures.adminUser({ role: "admin", promoted: true }));
    });

    renderApp({ api, route: "/users" });

    await user.click(await screen.findByRole("button", { name: "Make administrator" }));
    expect(await screen.findByText("User updated.")).toBeInTheDocument();
  });

  it("refuses to demote the last administrator, with the server's reason", async () => {
    const user = userEvent.setup();
    const api = usersApi([fixtures.adminUser({ role: "admin", promoted: true })]).on(
      "PATCH",
      "/api/v1/admin/users/{user_id}",
      () => problem(409, "last_admin"),
    );

    renderApp({ api, route: "/users" });

    await user.click(await screen.findByRole("button", { name: "Remove administrator" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "There has to be at least one enabled administrator.",
    );
  });

  it("asks before signing a user out of every device", async () => {
    const user = userEvent.setup();
    const api = usersApi().on("DELETE", "/api/v1/admin/users/{user_id}/sessions", () =>
      noContent(),
    );

    renderApp({ api, route: "/users" });

    await user.click(await screen.findByRole("button", { name: "Sign out everywhere" }));
    expect(await screen.findByText("Sign Bob out of every device?")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/admin/users/{user_id}/sessions")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("User updated.")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/admin/users/{user_id}/sessions")).toHaveLength(1);
  });

  it("sends a per-user daily limit, and the server default when it is cleared", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    const api = usersApi([fixtures.adminUser({ daily_generation_limit: 5 })]).on(
      "PATCH",
      "/api/v1/admin/users/{user_id}",
      (call) => {
        bodies.push(call.body);
        return ok(fixtures.adminUser());
      },
    );

    renderApp({ api, route: "/users" });

    const limit = await screen.findByLabelText("Daily generations for Bob");
    await user.clear(limit);
    await user.tab();

    expect(bodies).toEqual([{ daily_generation_limit: null }]);
  });
});
