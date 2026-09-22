import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../test/mock-api";
import { renderApp } from "../../test/render";
import { i18n } from "../../test/render";

function consoleApi(): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/admin/settings", () => ok(fixtures.settings()));
}

afterEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});

describe("the console layout", () => {
  it("shows the server name and the admin navigation", async () => {
    renderApp({ api: consoleApi(), route: "/settings" });

    expect(await screen.findByText("Maison")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Server settings" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });

  it("switches the language and remembers the choice", async () => {
    const user = userEvent.setup();
    renderApp({ api: consoleApi(), route: "/settings" });

    await user.selectOptions(await screen.findByLabelText("Language"), "fr");

    expect(await screen.findByRole("link", { name: "Réglages du serveur" })).toBeInTheDocument();
    expect(localStorage.getItem("tindarr.language")).toBe("fr");
  });

  it("switches the theme and remembers the choice", async () => {
    const user = userEvent.setup();
    renderApp({ api: consoleApi(), route: "/settings" });

    await user.selectOptions(await screen.findByLabelText("Theme"), "dark");

    await waitFor(() => {
      expect(document.documentElement.dataset["theme"]).toBe("dark");
    });
    expect(localStorage.getItem("tindarr.theme")).toBe("dark");

    await user.selectOptions(screen.getByLabelText("Theme"), "system");
    await waitFor(() => {
      expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    });
  });

  it("signs out and goes back to the sign-in page", async () => {
    const user = userEvent.setup();
    let signedIn = true;
    const api = consoleApi()
      .replace("GET", "/api/v1/auth/web/session", () =>
        signedIn ? ok(fixtures.webSession()) : problem(401, "unauthorized"),
      )
      .on("POST", "/api/v1/auth/logout", (call) => {
        expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
        signedIn = false;
        return noContent();
      });

    renderApp({ api, route: "/settings" });

    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows a not-found page for an unknown route", async () => {
    renderApp({ api: consoleApi(), route: "/nowhere" });

    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
