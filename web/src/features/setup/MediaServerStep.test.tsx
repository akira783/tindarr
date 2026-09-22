import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, problem } from "../../../test/mock-api";
import { renderWithProviders } from "../../../test/render";
import { MediaServerStep } from "./MediaServerStep";

describe("the media server step", () => {
  it("links the Plex owner account through a PIN, then saves with its handle", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("open", vi.fn());
    let authorized = false;
    const saved = vi.fn();
    const api = new MockApi()
      .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.setupSession()))
      .on("POST", "/api/v1/auth/plex/pins", (call) => {
        expect(call.body).toEqual({ purpose: "owner_token" });
        return ok(
          {
            pin_id: "pin-owner",
            auth_url: "https://app.plex.tv/auth#?clientID=x&code=y",
            expires_at: "2126-01-01T00:00:00Z",
          },
          201,
        );
      })
      .on("POST", "/api/v1/auth/plex/pins/status", () => {
        const answer = ok({
          status: authorized ? "authorized" : "pending",
          expires_at: "2126-01-01T00:00:00Z",
          account_name: authorized ? "ada@example.com" : null,
        });
        authorized = true;
        return answer;
      })
      .on("PUT", "/api/v1/setup/media-server", (call) => {
        expect(call.body).toEqual({
          connector: "media_server",
          server_type: "plex",
          url: "http://192.168.1.10:32400",
          verify_tls: true,
          plex_pin_id: "pin-owner",
        });
        return ok({ health: "ok", server_name: "Plex" });
      });

    renderWithProviders(<MediaServerStep state={fixtures.setupState()} onSaved={saved} />, { api });

    await user.selectOptions(screen.getByLabelText("Type"), "plex");
    await user.type(screen.getByLabelText("Address"), "http://192.168.1.10:32400");
    await user.click(screen.getByRole("button", { name: "Link the owner's Plex account" }));

    expect(
      await screen.findByText("Linked as ada@example.com", undefined, { timeout: 4000 }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Test and save" }));
    await waitFor(() => {
      expect(saved).toHaveBeenCalledWith({ health: "ok", server_name: "Plex" });
    });
    vi.unstubAllGlobals();
  });

  it("shows the coarse reason when the connection test fails", async () => {
    const user = userEvent.setup();
    const api = new MockApi()
      .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.setupSession()))
      .on("PUT", "/api/v1/setup/media-server", () => problem(502, "connector_unauthorized"));

    renderWithProviders(
      <MediaServerStep state={fixtures.setupState()} onSaved={vi.fn()} />,
      { api },
    );

    await user.type(screen.getByLabelText("Address"), "http://192.168.1.20:8096");
    await user.type(screen.getByLabelText("Administrator API key"), "wrong");
    await user.click(screen.getByRole("button", { name: "Test and save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The key or token was refused.");
  });

  it("marks a field the environment locks and leaves its secret out of the request", async () => {
    const user = userEvent.setup();
    const api = new MockApi()
      .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.setupSession()))
      .on("PUT", "/api/v1/setup/media-server", (call) => {
        expect(call.body).not.toHaveProperty("api_key");
        expect(call.body).not.toHaveProperty("verify_tls");
        return ok({ health: "ok" });
      });

    renderWithProviders(
      <MediaServerStep
        state={fixtures.setupState({ locked_fields: ["api_key", "verify_tls"] })}
        onSaved={vi.fn()}
      />,
      { api },
    );

    expect(screen.getByLabelText("Administrator API key")).toBeDisabled();
    await user.type(screen.getByLabelText("Address"), "http://192.168.1.20:8096");
    await user.click(screen.getByRole("button", { name: "Test and save" }));

    await waitFor(() => {
      expect(api.callsTo("PUT", "/api/v1/setup/media-server")).toHaveLength(1);
    });
  });
});
