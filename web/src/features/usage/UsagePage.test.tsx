import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function usageApi(days: Schemas[] = [fixtures.usageDay()]): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/admin/users", () => ok({ users: [fixtures.adminUser()] }))
    .on("GET", "/api/v1/admin/usage", () => ok({ days }));
}

type Schemas = ReturnType<typeof fixtures.usageDay>;

describe("the AI usage page", () => {
  it("shows a row per user and day, with the name rather than the id", async () => {
    renderApp({ api: usageApi(), route: "/usage" });

    const row = (await screen.findByRole("rowheader", { name: /24/ })).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("Bob")).toBeInTheDocument();
    expect(within(row!).getByText("1,200")).toBeInTheDocument();
  });

  it("falls back to the id for a user the list no longer holds", async () => {
    const api = usageApi([fixtures.usageDay({ user_id: "gone" })]);

    renderApp({ api, route: "/usage" });

    expect(await screen.findByText("gone")).toBeInTheDocument();
  });

  it("totals what the period cost, failures included", async () => {
    const api = usageApi([
      fixtures.usageDay({ generations: 3, input_tokens: 1000, output_tokens: 100 }),
      fixtures.usageDay({ date: "2026-09-23", generations: 2, input_tokens: 500, output_tokens: 50, failures: 1 }),
    ]);

    renderApp({ api, route: "/usage" });

    const batches = (await screen.findAllByText("Batches"))[0]!.closest(".stat");
    expect(batches).toHaveTextContent("5");
    expect(screen.getAllByText("Failures")[0]!.closest(".stat")).toHaveTextContent("1");
  });

  it("asks the server again when the period changes", async () => {
    const user = userEvent.setup();
    const asked: (string | null)[] = [];
    const api = new MockApi()
      .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
      .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
      .on("GET", "/api/v1/admin/users", () => ok({ users: [fixtures.adminUser()] }))
      .on("GET", "/api/v1/admin/usage", (call) => {
        asked.push(call.query.get("days"));
        return ok({ days: [fixtures.usageDay()] });
      });

    renderApp({ api, route: "/usage" });
    await screen.findByRole("rowheader", { name: /24/ });
    await user.selectOptions(screen.getByLabelText("Period"), "7");

    expect(asked).toEqual(["30", "7"]);
  });

  it("says so when nothing was generated", async () => {
    renderApp({ api: usageApi([]), route: "/usage" });

    expect(
      await screen.findByText("No batch has been generated in this period."),
    ).toBeInTheDocument();
  });

  it("shows the server's problem rather than an empty table", async () => {
    const api = usageApi().replace("GET", "/api/v1/admin/usage", () => problem(403, "forbidden"));

    renderApp({ api, route: "/usage" });

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
