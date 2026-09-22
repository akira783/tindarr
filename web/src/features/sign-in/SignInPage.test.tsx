import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, pending, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function anonymous(methods: ("password" | "plex_pin" | "quick_connect")[]): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo({ auth_methods: methods })))
    .on("GET", "/api/v1/auth/web/session", () => problem(401, "unauthorized"));
}

describe("signing in to the console", () => {
  it("signs in with a password and opens the console", async () => {
    const user = userEvent.setup();
    let signedIn = false;
    const api = anonymous(["password"])
      .replace("GET", "/api/v1/auth/web/session", () =>
        signedIn ? ok(fixtures.webSession()) : problem(401, "unauthorized"),
      )
      .on("POST", "/api/v1/auth/web/login", (call) => {
        expect(call.body).toEqual({ username: "ada", password: "secret" });
        signedIn = true;
        return ok(fixtures.webSession());
      })
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/sign-in" });

    await user.type(await screen.findByLabelText("User name"), "ada");
    await user.type(screen.getByLabelText("Password"), "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Server settings" })).toBeInTheDocument();
  });

  it("shows the same message for an unknown user and a wrong password", async () => {
    const user = userEvent.setup();
    const api = anonymous(["password"]).on("POST", "/api/v1/auth/web/login", () =>
      problem(401, "invalid_credentials"),
    );

    renderApp({ api, route: "/sign-in" });

    await user.type(await screen.findByLabelText("User name"), "nobody");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong user name or password.");
  });

  it("polls Quick Connect until the code is approved, then stops", async () => {
    const user = userEvent.setup();
    let polls = 0;
    let signedIn = false;
    const api = anonymous(["quick_connect"])
      .replace("GET", "/api/v1/auth/web/session", () =>
        signedIn ? ok(fixtures.webSession()) : problem(401, "unauthorized"),
      )
      .on("POST", "/api/v1/auth/quick-connect", (call) => {
        expect(call.body).toEqual({ purpose: "sign_in" });
        return ok({ handle: "h-1", code: "123456", expires_at: "2126-01-01T00:00:00Z" }, 201);
      })
      .on("POST", "/api/v1/auth/web/quick-connect/login", () => {
        polls += 1;
        if (polls < 3) return pending(250);
        signedIn = true;
        return ok(fixtures.webSession());
      })
      .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));

    renderApp({ api, route: "/sign-in" });

    await user.click(await screen.findByRole("button", { name: "Use Quick Connect" }));
    expect(await screen.findByText("123456")).toBeInTheDocument();

    expect(await screen.findByRole("heading", { name: "Server settings" }, { timeout: 3000 })).toBeInTheDocument();
    const pollsAtSignIn = polls;
    await new Promise((resolve) => setTimeout(resolve, 400));
    // Polling stopped: no call was made after the sign-in succeeded.
    expect(polls).toBe(pollsAtSignIn);
  });

  it("opens the plex.tv page in a window without an opener", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const api = anonymous(["plex_pin"])
      .on("POST", "/api/v1/auth/plex/pins", () =>
        ok(
          {
            pin_id: "pin-1",
            auth_url: "https://app.plex.tv/auth#?clientID=abc&code=XYZ",
            expires_at: "2126-01-01T00:00:00Z",
          },
          201,
        ),
      )
      .on("POST", "/api/v1/auth/web/plex/login", () => pending(250));

    renderApp({ api, route: "/sign-in" });

    await user.click(await screen.findByRole("button", { name: "Sign in with Plex" }));

    await waitFor(() => {
      expect(open).toHaveBeenCalledWith(
        "https://app.plex.tv/auth#?clientID=abc&code=XYZ",
        "_blank",
        "noopener,noreferrer",
      );
    });
    expect(screen.getByRole("link", { name: "Open the Plex page" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    vi.unstubAllGlobals();
  });

  it("never opens an address that is not a plex.tv page", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const api = anonymous(["plex_pin"]).on("POST", "/api/v1/auth/plex/pins", () =>
      ok(
        {
          pin_id: "pin-1",
          auth_url: "https://evil.example/auth",
          expires_at: "2126-01-01T00:00:00Z",
        },
        201,
      ),
    );

    renderApp({ api, route: "/sign-in" });

    await user.click(await screen.findByRole("button", { name: "Sign in with Plex" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("says so when the server offers no sign-in method", async () => {
    const api = anonymous([]);
    renderApp({ api, route: "/sign-in" });

    expect(
      await screen.findByText(
        "This server offers no sign-in method right now. Check the media server connector.",
      ),
    ).toBeInTheDocument();
  });
});
