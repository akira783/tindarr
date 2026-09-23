/**
 * The dialog answers every request that is waiting for it.
 *
 * Several calls can meet `403 reauth_required` at the same time — a page that saves
 * two things at once, or a mutation next to a refetch. They all wait on the one
 * dialog, so they must all be answered: a waiter that is dropped never settles, and
 * the call behind it hangs for ever.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../test/fixtures";
import { MockApi, ok } from "../../test/mock-api";
import { renderWithProviders } from "../../test/render";
import { requestReauth } from "./reauth-registry";

function signedIn(): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("POST", "/api/v1/auth/web/reauth", () =>
      ok({ reauth_expires_at: "2126-01-01T00:00:00Z" }),
    );
}

describe("the re-authentication dialog", () => {
  it("answers every request waiting on it, not only the last", async () => {
    const user = userEvent.setup();
    renderWithProviders(<p>console</p>, { api: signedIn() });
    await screen.findByText("console");

    const first = requestReauth();
    const second = requestReauth();
    const settled: string[] = [];
    void first.then((value) => settled.push(`first:${String(value)}`));
    void second.then((value) => settled.push(`second:${String(value)}`));

    await user.type(await screen.findByLabelText("Password for Ada"), "secret");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await first).toBe(true);
    expect(await second).toBe(true);
    await waitFor(() => {
      expect(settled).toHaveLength(2);
    });
  });

  it("answers the waiting requests when the dialog is dismissed", async () => {
    const user = userEvent.setup();
    renderWithProviders(<p>console</p>, { api: signedIn() });
    await screen.findByText("console");

    const first = requestReauth();
    const second = requestReauth();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(await first).toBe(false);
    expect(await second).toBe(false);
  });
});
