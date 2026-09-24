import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { MockApi, noContent, ok, pending, problem, type Call } from "../../../test/mock-api";
import { renderApp } from "../../../test/render";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];

interface Setup {
  status?: Partial<Schemas["SwipeStatus"]>;
  preferences?: Partial<Schemas["Preferences"]>;
  deck?: () => Response;
}

/** A console signed in as a user whose server has everything the deck needs. */
function deckApi(setup: Setup = {}): MockApi {
  return new MockApi()
    .on("GET", "/api/v1/server/info", () => ok(fixtures.serverInfo()))
    .on("GET", "/api/v1/auth/web/session", () => ok(fixtures.webSession()))
    .on("GET", "/api/v1/swipe/status", () => ok(fixtures.swipeStatus(setup.status ?? {})))
    .on("GET", "/api/v1/swipe/preferences", () => ok(fixtures.preferences(setup.preferences ?? {})))
    .on("PATCH", "/api/v1/swipe/preferences", (call) =>
      ok(fixtures.preferences({ ...(setup.preferences ?? {}), ...(call.body as object) })),
    )
    .on("GET", "/api/v1/swipe/deck", setup.deck ?? (() => ok(fixtures.deck())))
    .on("POST", "/api/v1/swipe/votes", () => ok({ results: [] }))
    .on("DELETE", "/api/v1/swipe/votes/{media_type}/{tmdb_id}", () => noContent());
}

function votesSent(api: MockApi): Schemas["VoteInput"][] {
  return api
    .callsTo("POST", "/api/v1/swipe/votes")
    .flatMap((call) => (call.body as { votes: Schemas["VoteInput"][] }).votes);
}

function deckCalls(api: MockApi): Call[] {
  return api.callsTo("GET", "/api/v1/swipe/deck");
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the deck's card", () => {
  it("shows the title, the length, the model's reason and the badges", async () => {
    renderApp({ api: deckApi(), route: "/deck" });

    expect(await screen.findByRole("heading", { name: /Inception/ })).toBeInTheDocument();
    expect(screen.getByText(/Heists inside dreams/)).toBeInTheDocument();
    expect(screen.getByText(/2 h 28/)).toBeInTheDocument();
    expect(screen.getByText("Your kind of thing")).toBeInTheDocument();
    // "On your services" is written out, never colour alone.
    expect(screen.getByText("On your services")).toBeInTheDocument();
    expect(screen.getByText("Netflix")).toBeInTheDocument();
    expect(screen.getByText("8.4/10")).toBeInTheDocument();
    expect(screen.getByText("87 %")).toBeInTheDocument();
  });

  it("announces the card it is showing, without reading the poster out", async () => {
    renderApp({ api: deckApi(), route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    const live = document.querySelector("[aria-live='polite']");
    expect(live).toHaveTextContent("Inception (2010)");
    expect(document.querySelector("img.poster")).toHaveAttribute("alt", "");
  });

  it("loads nothing from YouTube until the trailer is asked for", async () => {
    const user = userEvent.setup();
    renderApp({ api: deckApi(), route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    expect(document.querySelector("iframe")).toBeNull();

    await user.click(screen.getByRole("button", { name: /Watch the trailer/ }));

    const frame = document.querySelector("iframe");
    const src = new URL(frame?.getAttribute("src") ?? "");
    expect(src.origin).toBe("https://www.youtube-nocookie.com");
    expect(src.pathname).toBe("/embed/YoHD9XEInc0");
    expect(frame).toHaveAttribute("title", "Trailer for Inception");
  });

  it("shows the calibration progress while the engine is still learning", async () => {
    renderApp({ api: deckApi(), route: "/deck" });

    const bar = await screen.findByRole("progressbar", { name: "Getting to know you" });
    expect(bar).toHaveValue(12);
  });
});

describe("the five verdicts", () => {
  it("sends the verdict of each button, with the card's own id", async () => {
    const user = userEvent.setup();
    const api = deckApi({ status: { requests_enabled: false } });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.click(screen.getByRole("button", { name: /Seen it, liked it/ }));

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(1);
    });
    expect(votesSent(api)[0]).toMatchObject({ card_id: "card-1", vote: "seen_liked" });
  });

  it("fires the same verdicts from the keyboard", async () => {
    const user = userEvent.setup();
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () =>
        ok(
          fixtures.deck({
            cards: [
              fixtures.card({ id: "a", tmdb_id: 1 }),
              fixtures.card({ id: "b", tmdb_id: 2, title: "Arrival" }),
              fixtures.card({ id: "c", tmdb_id: 3, title: "Dune" }),
            ],
          }),
        ),
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("{ArrowLeft}");
    await screen.findByRole("heading", { name: /Arrival/ });
    await user.keyboard("{ArrowDown}");
    await screen.findByRole("heading", { name: /Dune/ });
    await user.keyboard("n");

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(3);
    });
    expect(votesSent(api).map((vote) => [vote.card_id, vote.vote])).toEqual([
      ["a", "dislike"],
      ["b", "seen_disliked"],
      ["c", "skip"],
    ]);
  });

  it("ignores a keystroke aimed at the mood field", async () => {
    const user = userEvent.setup();
    const api = deckApi({ status: { requests_enabled: false } });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.click(screen.getByLabelText("Tonight I feel like…"));
    await user.keyboard("n{ArrowLeft}");

    expect(votesSent(api)).toHaveLength(0);
    expect(screen.getByRole("heading", { name: /Inception/ })).toBeInTheDocument();
  });

  it("counts one verdict when two keystrokes land in the very same tick", async () => {
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () => ok(fixtures.deck({ cards: [fixtures.card({ id: "only", tmdb_id: 7 })] })),
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    // No render happens between the two: the handler's own state is a keystroke
    // behind, which is exactly the case a state-only guard misses.
    const region = document.querySelector(".deck-region");
    expect(region).not.toBeNull();
    await act(async () => {
      const press = (): void => {
        region?.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true }));
      };
      press();
      press();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(1);
    });
  });

  it("never votes twice on the same card, however fast the key repeats", async () => {
    const user = userEvent.setup();
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () => ok(fixtures.deck({ cards: [fixtures.card({ id: "only", tmdb_id: 7 })] })),
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("nnn");

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(1);
    });
  });
});

describe("undoing the last verdict", () => {
  it("puts the card back on top of the deck", async () => {
    const user = userEvent.setup();
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () =>
        ok(
          fixtures.deck({
            cards: [
              fixtures.card({ id: "a", tmdb_id: 1 }),
              fixtures.card({ id: "b", tmdb_id: 2, title: "Arrival" }),
            ],
          }),
        ),
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");
    await screen.findByRole("heading", { name: /Arrival/ });

    await user.click(await screen.findByRole("button", { name: /Undo “Inception”/ }));

    expect(await screen.findByRole("heading", { name: /Inception/ })).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/swipe/votes/movie/1")).toHaveLength(1);
  });

  it("is offered only once there is something to undo", async () => {
    renderApp({ api: deckApi({ status: { requests_enabled: false } }), route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    expect(screen.getByRole("button", { name: /Undo/ })).toBeDisabled();
  });
});

describe("leaving the deck and coming back", () => {
  it("does not offer a card again that this session has already judged", async () => {
    const user = userEvent.setup();
    const judged: string[] = [];
    // The server answers with the cards it still believes are unvoted, which is
    // what makes leaving and returning correct — as long as the console asks.
    const api = deckApi({ status: { requests_enabled: false } })
      .replace("GET", "/api/v1/swipe/deck", () =>
        ok(
          fixtures.deck({
            cards: [
              fixtures.card({ id: "a", tmdb_id: 1 }),
              fixtures.card({ id: "b", tmdb_id: 2, title: "Arrival" }),
            ].filter((one) => !judged.includes(one.id)),
          }),
        ),
      )
      .replace("POST", "/api/v1/swipe/votes", (call) => {
        for (const vote of (call.body as { votes: Schemas["VoteInput"][] }).votes) {
          judged.push(vote.card_id);
        }
        return ok({ results: [] });
      });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");
    await screen.findByRole("heading", { name: /Arrival/ });

    // Off to another page and back, well inside any cache lifetime.
    await user.click(screen.getByRole("link", { name: "My sessions" }));
    await screen.findByRole("heading", { name: "My sessions" });
    await user.click(screen.getByRole("link", { name: "Swipe" }));

    expect(await screen.findByRole("heading", { name: /Arrival/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Inception/ })).not.toBeInTheDocument();
  });

  it("keeps a verdict the server has not stored, and sends it on the way back", async () => {
    const user = userEvent.setup();
    let fail = true;
    const api = deckApi({ status: { requests_enabled: false } }).replace(
      "POST",
      "/api/v1/swipe/votes",
      () => {
        if (fail) {
          fail = false;
          throw new Error("the network dropped it");
        }
        return ok({ results: [] });
      },
    );
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");
    await screen.findByText(/1 verdict has not reached the server yet/);

    await user.click(screen.getByRole("link", { name: "My sessions" }));
    await screen.findByRole("heading", { name: "My sessions" });
    await user.click(screen.getByRole("link", { name: "Swipe" }));

    // The page is new; the verdict is not. It is still here, and still its own id.
    expect(
      await screen.findByText(/1 verdict has not reached the server yet/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send them again" }));

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(2);
    });
    expect(votesSent(api)[1]?.client_vote_id).toBe(votesSent(api)[0]?.client_vote_id);
  });
});

describe("a verdict the network loses", () => {
  it("keeps it, and resends it with the same id so the server counts one vote", async () => {
    const user = userEvent.setup();
    let fail = true;
    const api = deckApi({ status: { requests_enabled: false } }).replace(
      "POST",
      "/api/v1/swipe/votes",
      () => {
        if (fail) {
          fail = false;
          throw new Error("the network dropped it");
        }
        return ok({ results: [] });
      },
    );
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");

    expect(
      await screen.findByText(/1 verdict has not reached the server yet/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send them again" }));

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(2);
    });
    const [first, second] = votesSent(api);
    expect(second?.client_vote_id).toBe(first?.client_vote_id);
    await waitFor(() => {
      expect(screen.queryByText(/has not reached the server yet/)).not.toBeInTheDocument();
    });
  });

  it("still tells the server to undo it: the failure may have been on the way back", async () => {
    const user = userEvent.setup();
    const api = deckApi({ status: { requests_enabled: false } }).replace(
      "POST",
      "/api/v1/swipe/votes",
      () => {
        throw new Error("offline");
      },
    );
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");
    await screen.findByText(/1 verdict has not reached the server yet/);

    await user.click(screen.getByRole("button", { name: /Undo “Inception”/ }));

    // The card comes back at once — that is all the user asked for — and the
    // server is told too, because a submit can fail on the response leg with the
    // vote already stored. `DELETE` is idempotent and answers `404` when it was
    // not.
    expect(await screen.findByRole("heading", { name: /Inception/ })).toBeInTheDocument();
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/swipe/votes/movie/27205")).toHaveLength(1);
    });
    expect(screen.queryByText(/has not reached the server yet/)).not.toBeInTheDocument();
  });
});

describe("while a batch is being built", () => {
  it("waits what the server asked, then shows the cards and stops asking", async () => {
    let answered = 0;
    const api = deckApi({
      deck: () => {
        answered += 1;
        return answered === 1 ? pending(250) : ok(fixtures.deck());
      },
    });
    renderApp({ api, route: "/deck" });

    expect(await screen.findAllByText("Building your deck…")).not.toHaveLength(0);
    // The server asked for 250 ms; the deck's own floor is a second, because each
    // of these can start a generation (`DECK_MIN_POLL_MS`).
    expect(
      await screen.findByRole("heading", { name: /Inception/ }, { timeout: 4000 }),
    ).toBeInTheDocument();
    expect(deckCalls(api)).toHaveLength(2);

    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(deckCalls(api)).toHaveLength(2);
  });

  it("gives up rather than polling for ever, and says how to try again", async () => {
    vi.useFakeTimers();
    const api = deckApi({ deck: () => pending(1000) });
    renderApp({ api, route: "/deck" });

    await vi.advanceTimersByTimeAsync(200_000);
    expect(screen.getByRole("heading", { name: "Still nothing" })).toBeInTheDocument();

    const before = deckCalls(api).length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(deckCalls(api)).toHaveLength(before);
    // A bounded number of polls, not one per animation frame.
    expect(before).toBeLessThan(200);
  });
});

describe("the states the deck cannot leave on its own", () => {
  it("says what to do when no AI provider is configured, and asks for no deck", async () => {
    const api = deckApi({ status: { llm_configured: false } });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "No AI provider yet" })).toBeInTheDocument();
    expect(
      screen.getByText(/Add one on the connectors page, then come back here/),
    ).toBeInTheDocument();
    expect(deckCalls(api)).toHaveLength(0);
  });

  it("tells a plain user to ask an administrator instead", async () => {
    const api = deckApi({ status: { tmdb_configured: false } }).replace(
      "GET",
      "/api/v1/auth/web/session",
      () =>
        ok(
          fixtures.webSession({
            user: { id: "u9", name: "Bob", role: "user", media_server_admin: false },
          }),
        ),
    );
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "No TMDb key yet" })).toBeInTheDocument();
    expect(screen.getByText(/Ask an administrator of this server/)).toBeInTheDocument();
  });

  it("explains the daily cap rather than showing a raw error", async () => {
    const api = deckApi({
      deck: () => problem(429, "daily_limit_reached", { retry_after_ms: 3_600_000 }),
    });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "That is your batches for today" })).toBeInTheDocument();
    expect(screen.getByText(/Come back tomorrow/)).toBeInTheDocument();
  });

  it("explains that TMDb did not answer, and offers to try again", async () => {
    const api = deckApi({ deck: () => problem(503, "metadata_unreachable") });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "TMDb did not answer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("passes on what the AI provider failed with", async () => {
    const api = deckApi({ deck: () => problem(502, "llm_quota") });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "The AI provider failed" })).toBeInTheDocument();
    expect(screen.getByText(/quota is exhausted/)).toBeInTheDocument();
  });

  it("tells an exhausted pool apart from a batch on its way", async () => {
    const api = deckApi({ deck: () => ok(fixtures.deck({ cards: [], exhausted: true })) });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "Nothing left to offer" })).toBeInTheDocument();
    expect(screen.getByText(/Try a bolder setting/)).toBeInTheDocument();
  });

  it("offers another batch when the last one came back empty", async () => {
    const api = deckApi({ deck: () => ok(fixtures.deck({ cards: [] })) });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByRole("heading", { name: "No card this time" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask for more" })).toBeInTheDocument();
    expect(deckCalls(api)).toHaveLength(1);
  });

  it("asks for the next batch as the last card is judged", async () => {
    const user = userEvent.setup();
    let served = 0;
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () => {
        served += 1;
        return ok(
          fixtures.deck({
            cards: [fixtures.card({ id: `card-${served}`, tmdb_id: served, title: `Film ${served}` })],
          }),
        );
      },
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Film 1/ });
    expect(deckCalls(api)).toHaveLength(1);
    await user.keyboard("n");

    expect(await screen.findByRole("heading", { name: /Film 2/ })).toBeInTheDocument();
    expect(deckCalls(api)).toHaveLength(2);
  });

  it("stops and hands the button over when the refill brings nothing new", async () => {
    const user = userEvent.setup();
    // The server keeps serving the same card: a verdict of ours never reached it.
    const api = deckApi({
      status: { requests_enabled: false },
      deck: () => ok(fixtures.deck({ cards: [fixtures.card({ id: "stuck", tmdb_id: 42 })] })),
    });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");

    expect(await screen.findByRole("heading", { name: "The deck is empty" })).toBeInTheDocument();
    const asked = deckCalls(api).length;
    expect(asked).toBe(2);
    // And it stays there: nothing asks again by itself, because every ask can cost
    // a generation.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(deckCalls(api)).toHaveLength(asked);
    expect(screen.getByRole("button", { name: "Ask for more" })).toBeInTheDocument();
  });

  it("warns when most of what came back looks already seen", async () => {
    const api = deckApi({ deck: () => ok(fixtures.deck({ seen_ratio_warning: true })) });
    renderApp({ api, route: "/deck" });

    expect(await screen.findByText(/A lot of these look like titles you have already seen/)).toBeInTheDocument();
  });
});

describe("what the deck costs", () => {
  it("asks once, for the settings the user stored — not once for the defaults first", async () => {
    // The preferences are part of the query key, and each `GET /swipe/deck` with
    // nothing ready starts a generation charged to the daily cap. Answering
    // `status` first and `preferences` after is the ordinary case on a network.
    let releasePreferences = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      releasePreferences = resolve;
    });
    const api = deckApi({ preferences: { media_type: "movie", novelty: "bold" } }).replace(
      "GET",
      "/api/v1/swipe/preferences",
      async () => {
        await held;
        return ok(fixtures.preferences({ media_type: "movie", novelty: "bold" }));
      },
    );
    renderApp({ api, route: "/deck" });

    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/swipe/status")).toHaveLength(1);
    });
    expect(deckCalls(api)).toHaveLength(0);

    releasePreferences();
    await screen.findByRole("heading", { name: /Inception/ });
    expect(deckCalls(api)).toHaveLength(1);
    expect(deckCalls(api)[0]?.query.get("media_type")).toBe("movie");
    expect(deckCalls(api)[0]?.query.get("novelty")).toBe("bold");
  });

  it("says so when the preferences cannot be read, rather than silently using defaults", async () => {
    const api = deckApi().replace("GET", "/api/v1/swipe/preferences", () =>
      problem(500, "internal_error"),
    );
    renderApp({ api, route: "/deck" });

    expect(await screen.findByText(/unexpected error/)).toBeInTheDocument();
    expect(deckCalls(api)).toHaveLength(0);
  });
});

describe("the controls", () => {
  it("stores the new setting and asks for a deck that matches it", async () => {
    const user = userEvent.setup();
    const api = deckApi();
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.selectOptions(screen.getByLabelText("How adventurous"), "bold");

    await waitFor(() => {
      expect(deckCalls(api).at(-1)?.query.get("novelty")).toBe("bold");
    });
    expect(api.callsTo("PATCH", "/api/v1/swipe/preferences")[0]?.body).toEqual({ novelty: "bold" });
  });

  it("sends a mood only once it is submitted", async () => {
    const user = userEvent.setup();
    const api = deckApi();
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.type(screen.getByLabelText("Tonight I feel like…"), "something slow");
    expect(deckCalls(api).at(-1)?.query.get("mood")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Use this mood" }));
    await waitFor(() => {
      expect(deckCalls(api).at(-1)?.query.get("mood")).toBe("something slow");
    });
  });
});

describe("what happens after a like", () => {
  const answers: Schemas["RequestStatus"][] = [
    "queued",
    "awaiting_approval",
    "already_requested",
    "already_available",
  ];
  const sentences: Record<Schemas["RequestStatus"], RegExp> = {
    queued: /it is on its way/,
    awaiting_approval: /an administrator has to approve it/,
    already_requested: /had already been requested/,
    already_available: /already on your server/,
  };

  it.each(answers)("shows what the request backend answered: %s", async (status) => {
    const user = userEvent.setup();
    const api = deckApi().on("POST", "/api/v1/swipe/requests", () => ok({ request_status: status }));
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("{ArrowRight}");

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/“Inception” will be filed/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Request it" }));

    expect(await screen.findByText(sentences[status])).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/swipe/requests")[0]?.body).toEqual({
      media_type: "movie",
      tmdb_id: 27205,
    });
  });

  it("asks nothing and reports the answer when auto-request is on", async () => {
    const user = userEvent.setup();
    const api = deckApi({ preferences: { auto_request: true } }).replace(
      "POST",
      "/api/v1/swipe/votes",
      (call) =>
        ok({
          results: [
            {
              client_vote_id: (call.body as { votes: Schemas["VoteInput"][] }).votes[0]!
                .client_vote_id,
              outcome: "stored" as const,
              request_status: "queued" as const,
            },
          ],
        }),
    );
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("{ArrowRight}");

    expect(await screen.findByText(/“Inception”: Requested: it is on its way/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not offer a request at all when no backend is configured", async () => {
    const user = userEvent.setup();
    const api = deckApi({ status: { requests_enabled: false } });
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("{ArrowRight}");

    await waitFor(() => {
      expect(votesSent(api)).toHaveLength(1);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says so when the server refuses a verdict", async () => {
    const user = userEvent.setup();
    const api = deckApi({ status: { requests_enabled: false } }).replace(
      "POST",
      "/api/v1/swipe/votes",
      (call) =>
        ok({
          results: [
            {
              client_vote_id: (call.body as { votes: Schemas["VoteInput"][] }).votes[0]!
                .client_vote_id,
              outcome: "rejected" as const,
              code: "card_not_found",
            },
          ],
        }),
    );
    renderApp({ api, route: "/deck" });

    await screen.findByRole("heading", { name: /Inception/ });
    await user.keyboard("n");

    expect(await screen.findByText(/refused that verdict \(card_not_found\)/)).toBeInTheDocument();
  });
});
