import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, problem } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";

function pairingApi(): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () =>
      ok(fixtures.serverInfo({ auth_methods: ["password", "pairing"] })),
    )
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()));
}

describe("connecting a phone", () => {
  it("draws the QR code as SVG elements and names the host it connects to", async () => {
    const user = userEvent.setup();
    const api = pairingApi()
      .on("POST", "/api/v1/pairings", (call) => {
        expect(call.headers.get("X-CSRF-Token")).toBe("csrf-token");
        return ok(fixtures.newPairing(), 201);
      })
      .on("GET", "/api/v1/pairings/{pairing_id}", () => ok(fixtures.pairing()));

    const { container } = renderApp({ api, route: "/connect-phone" });

    await user.click(await screen.findByRole("button", { name: "Create a QR code" }));

    expect(await screen.findByText("Connects to tindeerr.example.com")).toBeInTheDocument();
    const svg = container.querySelector("svg[role='img']");
    expect(svg).not.toBeNull();
    expect(svg?.querySelectorAll("path").length).toBeGreaterThan(0);
    // The code itself is only in the QR image and the deep link, never in the text.
    expect(screen.getByRole("link", { name: "Open in Tindeerr" })).toHaveAttribute(
      "href",
      fixtures.newPairing().link,
    );
  });

  it("shows the phone, its address and the confirmation code, and approves it", async () => {
    const user = userEvent.setup();
    let approved = false;
    const api = pairingApi()
      .on("POST", "/api/v1/pairings", () => ok(fixtures.newPairing(), 201))
      .on("GET", "/api/v1/pairings/{pairing_id}", () =>
        ok(
          approved
            ? fixtures.pairing({
                status: "completed",
                device: { name: "Pixel 9", platform: "android" },
              })
            : fixtures.pairing({
                status: "awaiting_approval",
                device: { name: "Pixel 9", platform: "android" },
                requested_from: "192.168.1.31",
                confirmation_code: "4821",
              }),
        ),
      )
      .on("POST", "/api/v1/pairings/{pairing_id}/approve", () => {
        approved = true;
        return ok(fixtures.pairing({ status: "approved" }));
      });

    renderApp({ api, route: "/connect-phone" });

    await user.click(await screen.findByRole("button", { name: "Create a QR code" }));

    expect(await screen.findByText("Pixel 9")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.31")).toBeInTheDocument();
    expect(screen.getByText("Approve only if your phone shows 4821")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(
      await screen.findByText("Pixel 9 connected", undefined, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign this phone out" })).toBeInTheDocument();
  });

  it("rejects a pairing nobody recognises", async () => {
    const user = userEvent.setup();
    const api = pairingApi()
      .on("POST", "/api/v1/pairings", () => ok(fixtures.newPairing(), 201))
      .on("GET", "/api/v1/pairings/{pairing_id}", () =>
        ok(
          fixtures.pairing({
            status: "awaiting_approval",
            device: { name: "Unknown", platform: "android" },
            confirmation_code: "1111",
          }),
        ),
      )
      .on("DELETE", "/api/v1/pairings/{pairing_id}", () => noContent());

    renderApp({ api, route: "/connect-phone" });

    await user.click(await screen.findByRole("button", { name: "Create a QR code" }));
    await user.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/pairings/{pairing_id}")).toHaveLength(1);
    });
  });

  it("explains that the public address has to be set first", async () => {
    const user = userEvent.setup();
    const api = pairingApi().on("POST", "/api/v1/pairings", () =>
      problem(409, "public_url_not_set"),
    );

    renderApp({ api, route: "/connect-phone" });

    await user.click(await screen.findByRole("button", { name: "Create a QR code" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An administrator of the media server has to set the public address first.",
    );
  });
});
