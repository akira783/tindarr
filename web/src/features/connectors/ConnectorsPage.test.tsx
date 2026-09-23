import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function connectorsApi(connectors = fixtures.connectors()): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/admin/connectors", () => ok({ connectors }));
}

async function card(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: title, level: 2 });
  const section = heading.closest("section");
  expect(section).not.toBeNull();
  return section!;
}

describe("the connectors page", () => {
  it("shows every connector and what is known about it", async () => {
    const api = connectorsApi(
      fixtures.connectors({
        media_server: {
          configured: true,
          provider: "jellyfin",
          url: "http://jellyfin.lan:8096",
          secret: { set: true, last4: null },
          status: { health: "unknown" },
        },
        tmdb: {
          configured: true,
          secret: { set: true, last4: "cdef" },
          status: { health: "unknown" },
        },
      }),
    );

    renderApp({ api, route: "/connectors" });

    expect(await screen.findByRole("heading", { name: "Connectors", level: 1 })).toBeVisible();
    expect(within(await card("Media server")).getByText("http://jellyfin.lan:8096")).toBeVisible();
    // A Plex owner token is masked entirely; a media server key would show four.
    expect(
      within(await card("Media server")).getByText(/A key is stored/),
    ).toBeVisible();
    expect(within(await card("TMDb")).getByText(/ending in cdef/)).toBeVisible();
    expect(within(await card("OMDb")).getByText("Not configured.")).toBeVisible();
  });

  it("saves a metadata key and refreshes the list", async () => {
    const user = userEvent.setup();
    let saved: unknown = null;
    const api = connectorsApi().on("PUT", "/api/v1/admin/connectors/{kind}", (call) => {
      saved = call.body;
      expect(call.path).toBe("/api/v1/admin/connectors/tmdb");
      expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
      return ok(
        fixtures.connector("tmdb", {
          configured: true,
          secret: { set: true, last4: "cdef" },
          status: { health: "ok", checked_at: "2026-09-23T10:00:00Z" },
        }),
      );
    });

    renderApp({ api, route: "/connectors" });

    const tmdb = await card("TMDb");
    await user.type(within(tmdb).getByLabelText("API key"), "abcdef");
    await user.click(within(tmdb).getByRole("button", { name: "Save" }));

    expect(await within(tmdb).findByText("Connector saved.")).toBeVisible();
    expect(saved).toEqual({ connector: "tmdb", api_key: "abcdef" });
    expect(api.callsTo("GET", "/api/v1/admin/connectors")).toHaveLength(2);
  });

  it("tests a connector without saving it", async () => {
    const user = userEvent.setup();
    const api = connectorsApi().on("POST", "/api/v1/admin/connectors/{kind}/test", () =>
      ok<{ health: "ok"; checked_at: string; server_name: string }>({
        health: "ok",
        checked_at: "2026-09-23T10:00:00Z",
        server_name: "OMDb",
      }),
    );

    renderApp({ api, route: "/connectors" });

    const omdb = await card("OMDb");
    await user.type(within(omdb).getByLabelText("API key"), "omdb-key");
    await user.click(within(omdb).getByRole("button", { name: "Test the connection" }));

    expect(await within(omdb).findByText(/Works\./)).toBeVisible();
    expect(api.callsTo("POST", "/api/v1/admin/connectors/{kind}/test")[0]?.path).toBe(
      "/api/v1/admin/connectors/omdb/test",
    );
    expect(api.callsTo("POST", "/api/v1/admin/connectors/{kind}/test")).toHaveLength(1);
    expect(api.callsTo("PUT", "/api/v1/admin/connectors/{kind}")).toHaveLength(0);
  });

  it("says what the server said when a key is refused", async () => {
    const user = userEvent.setup();
    const api = connectorsApi().on("PUT", "/api/v1/admin/connectors/{kind}", () =>
      problem(502, "connector_unauthorized"),
    );

    renderApp({ api, route: "/connectors" });

    const tmdb = await card("TMDb");
    await user.type(within(tmdb).getByLabelText("API key"), "wrong");
    await user.click(within(tmdb).getByRole("button", { name: "Save" }));

    expect(await within(tmdb).findByRole("alert")).toHaveTextContent(/refused/i);
  });

  it("explains that a key cannot follow a connector to a new address", async () => {
    const user = userEvent.setup();
    const api = connectorsApi(
      fixtures.connectors({
        requests: {
          configured: true,
          url: "http://seerr.lan:5055",
          verify_tls: true,
          tv_seasons: "all",
          secret: { set: true, last4: "6789" },
          status: { health: "unknown" },
        },
      }),
    ).on("PUT", "/api/v1/admin/connectors/{kind}", () => problem(409, "secret_required"));

    renderApp({ api, route: "/connectors" });

    const requests = await card("Requests (Seerr)");
    const url = within(requests).getByLabelText("Address");
    await user.clear(url);
    await user.type(url, "http://elsewhere.lan:5055");
    await user.click(within(requests).getByRole("button", { name: "Save" }));

    expect(await within(requests).findByRole("alert")).toHaveTextContent(/key again/i);
  });

  it("sends only the request-backend fields that changed", async () => {
    const user = userEvent.setup();
    let saved: unknown = null;
    const api = connectorsApi(
      fixtures.connectors({
        requests: {
          configured: true,
          url: "http://seerr.lan:5055",
          verify_tls: true,
          tv_seasons: "all",
          secret: { set: true, last4: "6789" },
          status: { health: "unknown" },
        },
      }),
    ).on("PUT", "/api/v1/admin/connectors/{kind}", (call) => {
      saved = call.body;
      return ok(fixtures.connector("requests", { configured: true, status: { health: "ok" } }));
    });

    renderApp({ api, route: "/connectors" });

    const requests = await card("Requests (Seerr)");
    await user.selectOptions(
      within(requests).getByLabelText("Seasons requested for a series"),
      "first",
    );
    await user.click(within(requests).getByRole("button", { name: "Save" }));

    await within(requests).findByText("Connector saved.");
    // No key: the address did not move, so the stored one is reused.
    expect(saved).toEqual({
      connector: "requests",
      url: "http://seerr.lan:5055",
      verify_tls: true,
      tv_seasons: "first",
    });
  });

  it("removes a connector", async () => {
    const user = userEvent.setup();
    const api = connectorsApi(
      fixtures.connectors({
        omdb: { configured: true, secret: { set: true, last4: "cdef" }, status: { health: "unknown" } },
      }),
    ).on("DELETE", "/api/v1/admin/connectors/{kind}", (call) => {
      expect(call.path).toBe("/api/v1/admin/connectors/omdb");
      return noContent();
    });

    renderApp({ api, route: "/connectors" });

    await user.click(within(await card("OMDb")).getByRole("button", { name: "Remove" }));

    expect(api.callsTo("DELETE", "/api/v1/admin/connectors/{kind}")).toHaveLength(1);
  });

  it("shows a locked connector as locked and does not let it be edited", async () => {
    const api = connectorsApi(
      fixtures.connectors({
        tmdb: {
          configured: true,
          locked_fields: ["api_key"],
          secret: { set: true, last4: "cdef", locked: true },
          status: { health: "unknown" },
        },
      }),
    );

    renderApp({ api, route: "/connectors" });

    const tmdb = await card("TMDb");
    expect(await within(tmdb).findByLabelText("API key")).toBeDisabled();
    expect(within(tmdb).getAllByText(/Set by an environment variable/)).not.toHaveLength(0);
  });
});

describe("the AI provider card", () => {
  it("asks the provider for its models and fills the field from the list", async () => {
    const user = userEvent.setup();
    const api = connectorsApi().on("POST", "/api/v1/admin/llm/models", (call) => {
      expect(call.body).toEqual({
        connector: "llm",
        provider: "openai",
        api_key: "sk-test",
      });
      return ok({ models: ["model-a", "model-b"] });
    });

    renderApp({ api, route: "/connectors" });

    const llm = await card("AI provider");
    await user.type(within(llm).getByLabelText("API key"), "sk-test");
    await user.click(within(llm).getByRole("button", { name: "Load the provider's models" }));

    const picker = await within(llm).findByLabelText("Models this provider offers");
    await user.selectOptions(picker, "model-b");
    expect(within(llm).getByLabelText("Model")).toHaveValue("model-b");
  });

  it("lets a model be typed when the provider lists none", async () => {
    const user = userEvent.setup();
    let saved: unknown = null;
    const api = connectorsApi().on("PUT", "/api/v1/admin/connectors/{kind}", (call) => {
      saved = call.body;
      return ok(fixtures.connector("llm", { configured: true, status: { health: "ok" } }));
    });

    renderApp({ api, route: "/connectors" });

    const llm = await card("AI provider");
    await user.type(within(llm).getByLabelText("API key"), "sk-test");
    await user.type(within(llm).getByLabelText("Model"), "something-brand-new");
    await user.click(within(llm).getByRole("button", { name: "Save" }));

    await within(llm).findByText("Connector saved.");
    expect(saved).toEqual({
      connector: "llm",
      provider: "openai",
      api_key: "sk-test",
      model: "something-brand-new",
    });
  });

  it("asks for an address only for the providers that have one", async () => {
    const user = userEvent.setup();
    const api = connectorsApi();

    renderApp({ api, route: "/connectors" });

    const llm = await card("AI provider");
    expect(within(llm).queryByLabelText("Address")).toBeNull();

    await user.selectOptions(within(llm).getByLabelText("Provider"), "ollama");
    expect(within(llm).getByLabelText("Address")).toBeVisible();
    // Ollama has no accounts, so no key is asked for.
    expect(within(llm).queryByLabelText("API key")).toBeNull();
  });

  it("reports a provider that refuses the key", async () => {
    const user = userEvent.setup();
    const api = connectorsApi().on("POST", "/api/v1/admin/llm/models", () =>
      problem(502, "llm_auth_failed"),
    );

    renderApp({ api, route: "/connectors" });

    const llm = await card("AI provider");
    await user.type(within(llm).getByLabelText("API key"), "sk-wrong");
    await user.click(within(llm).getByRole("button", { name: "Load the provider's models" }));

    expect(await within(llm).findByRole("alert")).toHaveTextContent(/refused the key/i);
  });
});
