import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  getPreferences,
  getSwipeStatus,
  updatePreferences,
  type Card,
  type MediaFilter,
  type Novelty,
  type RequestStatus,
} from "../../api/operations";
import { isApiError } from "../../api/problem";
import { useAuth } from "../../auth/AuthProvider";
import { Alert, Button, ErrorAlert, Loading } from "../../components/ui";
import { posterUrl } from "../../lib/url";
import { AroundTheDeck } from "./AroundTheDeck";
import { DeckCard } from "./DeckCard";
import { DeckControls } from "./DeckControls";
import { isDeck } from "./deck-state";
import { actionForKey, SHORTCUTS, VERDICT_KEYS, VERDICTS } from "./keys";
import { RequestDialog } from "./RequestDialog";
import { useDeck } from "./useDeck";

/** A state the deck cannot leave on its own: a sentence, and what to do next. */
function DeadEnd({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}): ReactNode {
  return (
    <section className="card deck-state" aria-live="polite">
      <h2>{title}</h2>
      <p>{body}</p>
      {action}
    </section>
  );
}

/** Missing connector: the sentence depends on whether the reader can fix it. */
function NotConfigured({ what }: { what: "Provider" | "Tmdb" }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const { isAdmin } = useAuth();
  return (
    <DeadEnd
      title={t(`deck.states.no${what}Title`)}
      body={t(`deck.states.no${what}Body${isAdmin ? "Admin" : "User"}`)}
      action={
        isAdmin ? (
          <Link to="/connectors">{t("nav.connectors")}</Link>
        ) : undefined
      }
    />
  );
}

export function DeckPage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const { serverInfo } = useAuth();
  const queryClient = useQueryClient();

  const status = useQuery({
    queryKey: ["swipe", "status"] as const,
    queryFn: getSwipeStatus,
    retry: false,
  });
  const preferences = useQuery({
    queryKey: ["swipe", "preferences"] as const,
    queryFn: getPreferences,
    retry: false,
  });

  const [overrides, setOverrides] = useState<{
    mediaType?: MediaFilter;
    novelty?: Novelty;
  }>({});
  const [mood, setMood] = useState("");
  /** The card the trailer is open for: a new card closes it with no effect at all. */
  const [trailerFor, setTrailerFor] = useState<string | null>(null);
  const [requestOutcome, setRequestOutcome] = useState<{
    card: Card;
    status: RequestStatus;
  } | null>(null);

  const savePreference = useMutation({
    mutationFn: updatePreferences,
    onSuccess: (saved) => {
      queryClient.setQueryData(["swipe", "preferences"], saved);
    },
  });

  const swipeStatus = status.data;
  const mediaType =
    overrides.mediaType ?? preferences.data?.media_type ?? "both";
  const novelty = overrides.novelty ?? preferences.data?.novelty ?? "balanced";
  // The preferences are part of the deck's query key, so asking before they land
  // would ask twice: once for the defaults and once for what the user chose — and
  // each `GET /swipe/deck` with nothing ready starts a generation charged to the
  // daily cap.
  const configured =
    swipeStatus !== undefined &&
    swipeStatus.llm_configured &&
    swipeStatus.tmdb_configured &&
    preferences.data !== undefined;

  const deck = useDeck({
    settings: useMemo(
      () => ({ mediaType, novelty, mood }),
      [mediaType, novelty, mood],
    ),
    enabled: configured,
    requestsEnabled: swipeStatus?.requests_enabled ?? false,
    autoRequest: preferences.data?.auto_request ?? false,
  });

  const {
    current,
    next,
    vote,
    undo,
    canUndo,
    requestPrompt,
    closeRequestPrompt,
  } = deck;
  const answer = deck.query.data;
  const cards = isDeck(answer) ? answer : null;
  const total = deck.remaining.length;

  // --- the card that is showing ---------------------------------------------------

  const currentId = current?.id ?? null;
  const trailerOpen = trailerFor !== null && trailerFor === currentId;
  const toggleTrailer = useCallback(() => {
    setTrailerFor((open) => (open === currentId ? null : currentId));
  }, [currentId]);

  // The card, spoken when it changes. Only the title: the position would make a
  // count that drops with every vote talk over the card that has just arrived.
  const cardName =
    current === null
      ? ""
      : current.year == null
        ? current.title
        : `${current.title} (${current.year})`;

  // --- keyboard -----------------------------------------------------------------

  // Undo stays reachable when the deck has run dry: a wrong verdict on the last
  // card is exactly the one somebody wants back.
  const interactive = requestPrompt === null && (current !== null || canUndo);

  /**
   * The shortcuts belong to the deck, not to the document.
   *
   * A listener on `document` swallows the arrow keys everywhere, and this page is
   * long enough that a keyboard user would lose the only way they have of
   * scrolling it. Bound to the region instead — and the region takes focus when
   * the first card arrives, so the keyboard still works without a click.
   */
  const onRegionKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLElement>): void => {
      if (!interactive) return;
      const action = actionForKey({
        key: event.key,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        altKey: event.altKey,
        target: event.target,
        isComposing: event.nativeEvent.isComposing,
      });
      if (action === null) return;
      if (action === "undo") {
        if (!canUndo) return;
        event.preventDefault();
        undo();
        return;
      }
      if (action === "trailer") {
        if (current?.trailer == null) return;
        event.preventDefault();
        toggleTrailer();
        return;
      }
      if (current === null) return;
      event.preventDefault();
      vote(action);
    },
    [canUndo, current, interactive, toggleTrailer, undo, vote],
  );

  const regionRef = useRef<HTMLElement>(null);
  const hadCardRef = useRef(false);
  useEffect(() => {
    if (currentId === null || hadCardRef.current) return;
    hadCardRef.current = true;
    regionRef.current?.focus({ preventScroll: true });
  }, [currentId]);

  // The verdict buttons go away with the last card, taking the focus with them.
  // Undo is the one thing still worth doing, so it gets it.
  const undoRef = useRef<HTMLButtonElement>(null);
  const hadVerdictsRef = useRef(false);
  useEffect(() => {
    if (current !== null) {
      hadVerdictsRef.current = true;
      return;
    }
    if (!hadVerdictsRef.current) return;
    hadVerdictsRef.current = false;
    if (regionRef.current?.contains(document.activeElement) === true) return;
    undoRef.current?.focus();
  }, [current]);

  // The next poster, fetched while the user reads this one, so the deck never
  // shows an empty frame. It is a plain image fetch: nothing renders it.
  const nextPoster = posterUrl(
    serverInfo?.tmdb_image_base_url,
    next?.poster_path,
    "w342",
  );
  useEffect(() => {
    if (nextPoster === null || typeof Image !== "function") return;
    const image = new Image();
    image.src = nextPoster;
  }, [nextPoster]);

  const changeMedia = useCallback(
    (value: MediaFilter) => {
      setOverrides((current_) => ({ ...current_, mediaType: value }));
      savePreference.mutate({ media_type: value });
    },
    [savePreference],
  );
  const changeNovelty = useCallback(
    (value: Novelty) => {
      setOverrides((current_) => ({ ...current_, novelty: value }));
      savePreference.mutate({ novelty: value });
    },
    [savePreference],
  );

  // --- what to show where the card goes -------------------------------------------

  const error = deck.query.error;
  const code = isApiError(error) ? error.code : null;

  let body: ReactNode;
  /** What the live region says: the card, or the state that replaced it. */
  let live = cardName;
  if (status.isPending || preferences.isPending) {
    body = <Loading />;
  } else if (swipeStatus === undefined || preferences.data === undefined) {
    body = <ErrorAlert error={status.error ?? preferences.error} />;
  } else if (!swipeStatus.llm_configured) {
    body = <NotConfigured what="Provider" />;
  } else if (!swipeStatus.tmdb_configured) {
    body = <NotConfigured what="Tmdb" />;
  } else if (code === "llm_not_configured") {
    body = <NotConfigured what="Provider" />;
  } else if (code === "tmdb_not_configured") {
    body = <NotConfigured what="Tmdb" />;
  } else if (code === "daily_limit_reached") {
    live = t("deck.states.dailyLimitTitle");
    body = (
      <DeadEnd
        title={t("deck.states.dailyLimitTitle")}
        body={t("deck.states.dailyLimitBody")}
      />
    );
  } else if (code === "metadata_unreachable") {
    live = t("deck.states.metadataTitle");
    body = (
      <DeadEnd
        title={t("deck.states.metadataTitle")}
        body={t("deck.states.metadataBody")}
        action={
          <Button onClick={deck.retry}>{t("common:actions.retry")}</Button>
        }
      />
    );
  } else if (
    code === "llm_auth_failed" ||
    code === "llm_quota" ||
    code === "llm_model_not_found" ||
    code === "llm_unreachable" ||
    code === "llm_invalid_output"
  ) {
    live = t("deck.states.llmTitle");
    body = (
      <DeadEnd
        title={t("deck.states.llmTitle")}
        body={`${t(`common:errors.${code}`)} ${t("deck.states.llmBody")}`}
        action={
          <Button onClick={deck.retry}>{t("common:actions.retry")}</Button>
        }
      />
    );
  } else if (error !== null) {
    body = (
      <section className="card deck-state">
        <ErrorAlert error={error} />
        <Button onClick={deck.retry}>{t("common:actions.retry")}</Button>
      </section>
    );
  } else if (deck.gaveUp) {
    live = t("deck.states.tooLongTitle");
    body = (
      <DeadEnd
        title={t("deck.states.tooLongTitle")}
        body={t("deck.states.tooLongBody")}
        action={
          <Button onClick={deck.retry}>{t("common:actions.retry")}</Button>
        }
      />
    );
  } else if (deck.generating || deck.query.isPending) {
    live = t("deck.states.generating");
    body = (
      <section className="card deck-state">
        <p role="status">{t("deck.states.generating")}</p>
        <p className="hint">{t("deck.states.generatingHint")}</p>
      </section>
    );
  } else if (current !== null) {
    body = (
      <DeckCard
        card={current}
        position={1}
        total={total}
        trailerOpen={trailerOpen}
        onToggleTrailer={toggleTrailer}
      />
    );
  } else if (cards?.exhausted === true) {
    live = t("deck.states.exhaustedTitle");
    body = (
      <DeadEnd
        title={t("deck.states.exhaustedTitle")}
        body={t("deck.states.exhaustedBody")}
      />
    );
  } else if (deck.stalled) {
    live = t("deck.states.stalledTitle");
    body = (
      <DeadEnd
        title={t("deck.states.stalledTitle")}
        body={t("deck.states.stalledBody")}
        action={<Button onClick={deck.retry}>{t("deck.states.more")}</Button>}
      />
    );
  } else {
    // Everything else has been named above: no data with no error and no pending
    // query cannot happen, since a query with neither is `isPending`.
    live = t("deck.states.emptyTitle");
    body = (
      <DeadEnd
        title={t("deck.states.emptyTitle")}
        body={t("deck.states.emptyBody")}
        action={<Button onClick={deck.retry}>{t("deck.states.more")}</Button>}
      />
    );
  }

  const left = swipeStatus?.generations_left_today;

  return (
    <main id="main" className="page deck-page">
      <h1>{t("deck.title")}</h1>
      <p>{t("deck.intro")}</p>
      <p className="hint">
        {left == null ? null : `${t("deck.generationsLeft", { count: left })} `}
        {swipeStatus?.streaming_region == null
          ? null
          : t("deck.region", { region: swipeStatus.streaming_region })}
      </p>

      <DeckControls
        mediaType={mediaType}
        novelty={novelty}
        mood={mood}
        busy={deck.query.isFetching}
        calibration={cards?.calibration ?? swipeStatus?.calibration ?? null}
        onMediaType={changeMedia}
        onNovelty={changeNovelty}
        onMood={setMood}
      />

      {cards?.seen_ratio_warning === true && (
        <Alert kind="warning">{t("deck.states.seenWarning")}</Alert>
      )}

      {/* Always mounted, so a screen reader is already watching it when the card
          — or the state that replaced the card — changes. A live region created
          with its text already in it is not reliably announced. */}
      <p className="visually-hidden" role="status" aria-live="polite">
        {live}
      </p>

      {/* The deck itself: the card and what can be done to it. The shortcuts are
          bound here rather than to the document, so the arrow keys still scroll
          this long page everywhere else.

          The rule below is about listeners on elements nobody can reach. This one
          is in the tab order, takes focus when the first card arrives, and every
          shortcut it answers to is also a button inside it carrying the same
          `aria-keyshortcuts` — so nothing here is reachable by keyboard only. */}
      {/* eslint-disable-next-line jsx-a11y-x/no-noninteractive-element-interactions */}
      <section
        ref={regionRef}
        className="deck-region"
        tabIndex={-1}
        aria-label={t("deck.card.region")}
        onKeyDown={onRegionKeyDown}
      >
        {body}

        {(current !== null || deck.lastVote !== null) && (
          <div className="deck-actions">
          <div className="row verdicts">
            {current === null
              ? null
              : VERDICTS.map((value) => (
                  <Button
                    key={value}
                    variant={value === "like" ? "primary" : "secondary"}
                    aria-keyshortcuts={SHORTCUTS[value]}
                    onClick={() => {
                      vote(value);
                    }}
                  >
                    {t(`deck.verdict.${VERDICT_KEYS[value]}`)}{" "}
                    <span className="badge">
                      {t(`deck.keys.${VERDICT_KEYS[value]}`)}
                    </span>
                  </Button>
                ))}
          </div>
          <div className="row">
            <Button
              ref={undoRef}
              disabled={!canUndo}
              aria-keyshortcuts={SHORTCUTS.undo}
              onClick={() => {
                undo();
              }}
            >
              {deck.lastVote === null
                ? t("deck.undo")
                : t("deck.undoOf", { title: deck.lastVote.card.title })}{" "}
              <span className="badge">{t("deck.keys.undo")}</span>
            </Button>
            <span className="hint">{t("deck.undoHint")}</span>
          </div>
            <p className="hint">{t("deck.keys.hint")}</p>
          </div>
        )}
      </section>

      {deck.unsent.length > 0 && (
        <Alert kind="warning">
          {t("deck.votes.unsent", { count: deck.unsent.length })}{" "}
          <Button disabled={deck.sending} onClick={deck.retryUnsent}>
            {deck.sending ? t("deck.votes.sending") : t("deck.votes.retry")}
          </Button>
        </Alert>
      )}
      {deck.rejected.map((rejectedCode) => (
        <Alert kind="error" key={rejectedCode}>
          {t("deck.votes.rejected", { code: rejectedCode })}
        </Alert>
      ))}
      {deck.voteError !== null && <ErrorAlert error={deck.voteError} />}

      {deck.autoRequested !== null && (
        <Alert kind="success">
          {t("deck.request.auto", {
            title: deck.autoRequested.card.title,
            status: t(`deck.request.status.${deck.autoRequested.status}`),
          })}
        </Alert>
      )}
      {requestOutcome !== null && (
        <Alert kind="success">
          {t("deck.request.auto", {
            title: requestOutcome.card.title,
            status: t(`deck.request.status.${requestOutcome.status}`),
          })}
        </Alert>
      )}

      <RequestDialog
        card={requestPrompt}
        onClose={closeRequestPrompt}
        onFiled={(card, requestStatus) => {
          setRequestOutcome({ card, status: requestStatus });
        }}
      />

      <AroundTheDeck />
    </main>
  );
}
