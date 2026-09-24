import { describe, expect, it } from "vitest";
import userEvent from "@testing-library/user-event";
import { screen, waitFor } from "@testing-library/react";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];

function poster(tmdbId: number, title: string): Schemas["GridTitle"] {
  return {
    media_type: "movie",
    tmdb_id: tmdbId,
    title,
    year: 2010,
    poster_path: `/${tmdbId}.jpg`,
  };
}

function gridApi(titles: Schemas["GridTitle"][]): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/swipe/calibration/grid", () => ok({ page: 1, titles }));
}

describe("the calibration grid", () => {
  it("shows a poster per title, each one a switch", async () => {
    const api = gridApi([poster(27205, "Inception"), poster(155, "The Dark Knight")]);
    renderApp({ api, route: "/already-seen" });

    const tiles = await screen.findAllByRole("button", { pressed: false });
    expect(tiles.map((tile) => tile.textContent)).toContain("Inception (2010)");
    // The poster is decorative — an empty alt, so it carries no role of its own — and
    // the title beside it is what a screen reader announces with the button.
    const images = [...document.querySelectorAll("img")];
    expect(images).toHaveLength(2);
    expect(images.every((image) => image.getAttribute("alt") === "")).toBe(true);
    expect(screen.queryAllByRole("img")).toHaveLength(0);
  });

  it("sends a seen flag for every poster, ticked or not", async () => {
    const user = userEvent.setup();
    let sent: unknown = null;
    const api = gridApi([poster(27205, "Inception"), poster(155, "The Dark Knight")]).on(
      "POST",
      "/api/v1/swipe/calibration/grid",
      (call) => {
        sent = call.body;
        expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
        return ok({ recorded: 2 });
      },
    );
    renderApp({ api, route: "/already-seen" });

    await user.click(await screen.findByRole("button", { name: /Inception/ }));
    await user.click(screen.getByRole("button", { name: "Save (1)" }));

    await waitFor(() => {
      expect(sent).toEqual({
        answers: [
          { media_type: "movie", tmdb_id: 27205, seen: true },
          { media_type: "movie", tmdb_id: 155, seen: false },
        ],
      });
    });
    expect(await screen.findByText("2 answers saved.")).toBeVisible();
  });

  it("says so when TMDb is not configured", async () => {
    const api = new MockApi()
      .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
      .on("GET", "/api/v1/swipe/calibration/grid", () => problem(409, "tmdb_not_configured"));
    renderApp({ api, route: "/already-seen" });

    expect(await screen.findByRole("alert")).toBeVisible();
  });

  it("has nothing to ask when the wall comes back empty", async () => {
    renderApp({ api: gridApi([]), route: "/already-seen" });
    expect(await screen.findByText("No poster to show. Check that TMDb is configured.")).toBeVisible();
  });
});
