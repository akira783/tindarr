import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function aroundApi(): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/swipe/status", () => ok(fixtures.swipeStatus()))
    .on("GET", "/api/v1/swipe/preferences", () => ok(fixtures.preferences()))
    .on("GET", "/api/v1/swipe/deck", () => ok(fixtures.deck()))
    .on("GET", "/api/v1/swipe/likes", () => ok({ likes: [fixtures.like()], next_cursor: null }))
    .on("GET", "/api/v1/swipe/profile", () => ok(fixtures.profileState()))
    .on("PUT", "/api/v1/swipe/profile", (call) =>
      ok(
        fixtures.profileState({
          profile: {
            text: (call.body as { text: string }).text,
            user_edited: true,
            updated_at: "2026-09-24T10:00:00Z",
            votes_since_update: 0,
          },
        }),
      ),
    )
    .on("POST", "/api/v1/swipe/profile/refresh", () => ok({ started: true }))
    .on("GET", "/api/v1/swipe/stats", () => ok(fixtures.stats()))
    .on("POST", "/api/v1/swipe/requests", () => ok({ request_status: "queued" }));
}

async function open(name: string): Promise<HTMLElement> {
  const user = userEvent.setup();
  const summary = await screen.findByText(name);
  await user.click(summary);
  const panel = summary.closest("details");
  expect(panel).not.toBeNull();
  return panel as HTMLElement;
}

describe("what sits around the deck", () => {
  it("costs nothing until a panel is opened", async () => {
    const api = aroundApi();
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    expect(api.callsTo("GET", "/api/v1/swipe/likes")).toHaveLength(0);
    expect(api.callsTo("GET", "/api/v1/swipe/profile")).toHaveLength(0);
    expect(api.callsTo("GET", "/api/v1/swipe/stats")).toHaveLength(0);
  });

  it("lists the likes and files a request for one", async () => {
    const user = userEvent.setup();
    const api = aroundApi();
    renderApp({ api, route: "/deck" });
    await screen.findByRole("heading", { name: /Inception/ });

    const panel = await open("My likes");
    expect(await within(panel).findByText("Inception (2010)")).toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "Request" }));
    expect(await within(panel).findByText(/it is on its way/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/swipe/requests")[0]?.body).toEqual({
      media_type: "movie",
      tmdb_id: 27205,
    });
  });

  it("asks the server only for the likes the filter names", async () => {
    const user = userEvent.setup();
    const api = aroundApi();
    renderApp({ api, route: "/deck" });
    await screen.findByRole("heading", { name: /Inception/ });

    const panel = await open("My likes");
    await within(panel).findByText("Inception (2010)");
    await user.click(within(panel).getByRole("button", { name: "To request" }));

    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/swipe/likes").at(-1)?.query.get("status")).toBe(
        "to_request",
      );
    });
  });

  it("shows the taste profile and keeps what the user writes", async () => {
    const user = userEvent.setup();
    const api = aroundApi();
    renderApp({ api, route: "/deck" });
    await screen.findByRole("heading", { name: /Inception/ });

    const panel = await open("My taste");
    expect(await within(panel).findByText(/Loves: heists/)).toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "Edit" }));
    const field = within(panel).getByRole("textbox");
    await user.clear(field);
    await user.type(field, "No musicals, ever.");
    await user.click(within(panel).getByRole("button", { name: "Save" }));

    expect(await within(panel).findByText("No musicals, ever.")).toBeInTheDocument();
    expect(within(panel).getByText(/Written by you/)).toBeInTheDocument();
    expect(api.callsTo("PUT", "/api/v1/swipe/profile")[0]?.body).toEqual({
      text: "No musicals, ever.",
    });
  });

  it("asks for a rewrite and says it is running", async () => {
    const user = userEvent.setup();
    const api = aroundApi().replace("GET", "/api/v1/swipe/profile", () => {
      const asked = api.callsTo("GET", "/api/v1/swipe/profile").length;
      return ok(fixtures.profileState({ refreshing: asked > 1 }));
    });
    renderApp({ api, route: "/deck" });
    await screen.findByRole("heading", { name: /Inception/ });

    const panel = await open("My taste");
    await within(panel).findByText(/Loves: heists/);
    await user.click(within(panel).getByRole("button", { name: "Rewrite it from my votes" }));

    expect(await within(panel).findByText("Being rewritten…")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/swipe/profile/refresh")).toHaveLength(1);
  });

  it("counts the session's votes, skips apart", async () => {
    const api = aroundApi();
    renderApp({ api, route: "/deck" });
    await screen.findByRole("heading", { name: /Inception/ });

    const panel = await open("My numbers");
    const votes = (await within(panel).findByText("Votes")).closest(".stat");
    expect(votes).toHaveTextContent("10");
    expect(within(panel).getByText("Put off").closest(".stat")).toHaveTextContent("2");
    expect(within(panel).getByText("Like rate").closest(".stat")).toHaveTextContent("40%");
    expect(within(panel).getByText(/Your kind of thing — 3 liked out of 6/)).toBeInTheDocument();
  });
});
