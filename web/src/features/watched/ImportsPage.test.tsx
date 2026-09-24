import { describe, expect, it } from "vitest";
import userEvent from "@testing-library/user-event";
import { screen, waitFor, within } from "@testing-library/react";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];

function importRecord(overrides: Partial<Schemas["Import"]> = {}): Schemas["Import"] {
  return {
    id: "imp-1",
    format: "netflix",
    status: "complete",
    created_at: "2026-09-24T10:00:00Z",
    finished_at: "2026-09-24T10:01:00Z",
    titles: 72,
    matched: 70,
    queued: 2,
    skipped: { supplemental: 1 },
    error_code: null,
    ...overrides,
  };
}

function reviewEntry(
  overrides: Partial<Schemas["ImportReviewEntry"]> = {},
): Schemas["ImportReviewEntry"] {
  return {
    id: "entry-1",
    query: "Mushoku Tensei",
    media_type: "tv",
    episodes: 1,
    rating: null,
    last_watched_at: null,
    candidates: [
      {
        media_type: "tv",
        tmdb_id: 94664,
        title: "Mushoku Tensei: Jobless Reincarnation",
        year: 2021,
        poster_path: "/a.jpg",
        similarity: 0.56,
      },
    ],
    ...overrides,
  };
}

function importsApi(imports: Schemas["Import"][] = []): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/swipe/imports", () => ok({ imports }));
}

describe("the imports page", () => {
  it("uploads the file as the body, with the CSRF token", async () => {
    const user = userEvent.setup();
    let sent: unknown = null;
    const api = importsApi().on("POST", "/api/v1/swipe/imports", (call) => {
      sent = call.body;
      expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
      return ok(importRecord({ status: "running", matched: 0, queued: 0 }), 202);
    });
    renderApp({ api, route: "/imports" });

    const input = await screen.findByLabelText("Export file");
    await user.upload(
      input,
      new File(['Title,Date\n"Heroes: Saison 1: A","9/10/26"\n'], "history.csv", {
        type: "text/csv",
      }),
    );

    expect(await screen.findByText("Uploaded. Identifying the titles takes a moment.")).toBeVisible();
    // The body is the file itself: no form, no boundary, nothing named after it.
    expect(sent).toContain("Heroes: Saison 1: A");
  });

  it("says what the import found and what it dropped", async () => {
    const api = importsApi([importRecord()]);
    renderApp({ api, route: "/imports" });

    expect(await screen.findByText("72 titles read, 70 identified, 2 to check.")).toBeVisible();
    expect(screen.getByText("1 trailer dropped")).toBeVisible();
    expect(screen.getByText("Netflix viewing history")).toBeVisible();
  });

  it("explains a failure with a sentence and never with a code", async () => {
    const api = importsApi([
      importRecord({ status: "failed", error_code: "metadata_unreachable", queued: 0 }),
    ]);
    renderApp({ api, route: "/imports" });

    expect(
      await screen.findByText(
        "TMDb stopped answering, so the rest of the file was left alone. Try again later.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/metadata_unreachable/)).toBeNull();
  });

  it("shows a message for a CSV the server does not recognise", async () => {
    const user = userEvent.setup();
    const api = importsApi().on("POST", "/api/v1/swipe/imports", () =>
      problem(400, "import_unreadable"),
    );
    renderApp({ api, route: "/imports" });

    await user.upload(
      await screen.findByLabelText("Export file"),
      new File(["who,what\n1,2\n"], "notes.csv", { type: "text/csv" }),
    );
    expect(await screen.findByRole("alert")).toBeVisible();
  });

  it("accepts one of the titles a queued row offered", async () => {
    const user = userEvent.setup();
    let decided: unknown = null;
    const api = importsApi([importRecord()])
      .on("GET", "/api/v1/swipe/imports/{import_id}/review", () =>
        ok({ entries: [reviewEntry()], pending: 1 }),
      )
      .on("POST", "/api/v1/swipe/imports/{import_id}/review/{entry_id}", (call) => {
        decided = call.body;
        return noContent();
      });
    renderApp({ api, route: "/imports" });

    await user.click(await screen.findByRole("button", { name: "Check 2 titles" }));
    expect(await screen.findByText("Mushoku Tensei")).toBeVisible();
    const candidate = screen.getByText("Mushoku Tensei: Jobless Reincarnation").closest("li");
    expect(candidate).not.toBeNull();
    await user.click(within(candidate as HTMLElement).getByRole("button", { name: "This one" }));

    await waitFor(() => {
      expect(decided).toEqual({
        decision: "accept",
        title: { media_type: "tv", tmdb_id: 94664 },
      });
    });
  });

  it("rejects a row whose candidates are all wrong", async () => {
    const user = userEvent.setup();
    let decided: unknown = null;
    const api = importsApi([importRecord()])
      .on("GET", "/api/v1/swipe/imports/{import_id}/review", () =>
        ok({ entries: [reviewEntry({ candidates: [] })], pending: 1 }),
      )
      .on("POST", "/api/v1/swipe/imports/{import_id}/review/{entry_id}", (call) => {
        decided = call.body;
        return noContent();
      });
    renderApp({ api, route: "/imports" });

    await user.click(await screen.findByRole("button", { name: "Check 2 titles" }));
    expect(await screen.findByText("TMDb knew nothing like this row.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "None of these" }));

    await waitFor(() => {
      expect(decided).toEqual({ decision: "reject" });
    });
  });

  it("forgets an import once the warning is confirmed", async () => {
    const user = userEvent.setup();
    const api = importsApi([importRecord()]).on(
      "DELETE",
      "/api/v1/swipe/imports/{import_id}",
      () => noContent(),
    );
    renderApp({ api, route: "/imports" });

    await user.click(await screen.findByRole("button", { name: "Forget this import" }));
    await user.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/swipe/imports/imp-1")).toHaveLength(1);
    });
  });
});
