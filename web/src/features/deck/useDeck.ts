import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getDeck,
  submitVotes,
  undoVote,
  type Card,
  type MediaFilter,
  type Novelty,
  type RequestStatus,
  type VoteValue,
} from "../../api/operations";
import { isApiError } from "../../api/problem";
import {
  DECK_MIN_POLL_MS,
  isDeck,
  isPendingBatch,
  MAX_WAIT_MS,
  newVoteId,
  pollDelay,
  remainingCards,
  toVoteInput,
  type DeckAnswer,
  type PendingVote,
} from "./deck-state";
import {
  getVoteQueue,
  markAttempted,
  setVoteQueue,
  wasAttempted,
} from "./vote-queue";

export interface DeckSettings {
  mediaType: MediaFilter;
  novelty: Novelty;
  /** A free-text wish for this session; empty means "no mood". */
  mood: string;
}

export interface DeckOptions {
  settings: DeckSettings;
  /** False until the server says an AI provider and TMDb are configured. */
  enabled: boolean;
  requestsEnabled: boolean;
  /** `Preferences.auto_request`: a like files the request without asking. */
  autoRequest: boolean;
}

export interface AutoRequestOutcome {
  card: Card;
  status: RequestStatus;
}

export interface DeckController {
  query: UseQueryResult<DeckAnswer>;
  /** The cards left to show, in the server's order. */
  remaining: Card[];
  current: Card | null;
  next: Card | null;
  /** A batch is being generated and the console is polling for it. */
  generating: boolean;
  /** The generation took longer than the console is willing to wait. */
  gaveUp: boolean;
  /** Cards were served, all of them have been voted on, and nothing new came back. */
  stalled: boolean;
  vote: (value: VoteValue) => void;
  undo: () => void;
  canUndo: boolean;
  lastVote: PendingVote | null;
  sending: boolean;
  /** Votes the server has not stored yet. They are never dropped. */
  unsent: PendingVote[];
  retryUnsent: () => void;
  voteError: unknown;
  rejected: string[];
  requestPrompt: Card | null;
  closeRequestPrompt: () => void;
  /** What the request backend answered to a request filed automatically. */
  autoRequested: AutoRequestOutcome | null;
  retry: () => void;
}

/**
 * The deck: one query, one vote queue, one way back.
 *
 * **Polling** is the query's own `refetchInterval`, so it ends with the
 * component, with the tab, on the first batch, on any error and on a deadline —
 * rather than a loop that has to remember to stop.
 *
 * **Refilling is never automatic.** The deck asks for more only just after a vote
 * emptied it, or when the user presses the button. A card the server still
 * believes is unvoted — a verdict that never arrived — would otherwise be an
 * endless `GET /swipe/deck`, and each of those starts a generation charged to the
 * user's daily cap.
 *
 * **Votes** go into a queue that only a stored answer empties: a network that
 * drops one keeps it, with the same `client_vote_id`, so the resend is a
 * `duplicate` and never a second vote.
 */
export function useDeck(options: DeckOptions): DeckController {
  const { settings, enabled, requestsEnabled, autoRequest } = options;
  const queryClient = useQueryClient();

  const [voted, setVoted] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [queue, setQueue] = useState<PendingVote[]>(getVoteQueue);
  const [lastVote, setLastVote] = useState<PendingVote | null>(null);
  const [sending, setSending] = useState(false);
  const [voteError, setVoteError] = useState<unknown>(null);
  const [rejected, setRejected] = useState<string[]>([]);
  const [requestPrompt, setRequestPrompt] = useState<Card | null>(null);
  const [autoRequested, setAutoRequested] = useState<AutoRequestOutcome | null>(null);
  const [gaveUp, setGaveUp] = useState(false);
  /** A refill the last verdict asked for and its submission has not released yet. */
  const [refilling, setRefilling] = useState(false);
  /** Bumped by every manual retry, so the deadline is armed again. */
  const [attempt, setAttempt] = useState(0);

  const queueRef = useRef<PendingVote[]>(getVoteQueue());
  const chainRef = useRef<Promise<void>>(Promise.resolve());
  /**
   * The same set as `voted`, kept in step but readable now.
   *
   * Two keystrokes inside one tick see the same render, so the state and the
   * `remaining` list a handler closes over are both a keystroke behind: without
   * this the second one would send a second verdict on the card the first has
   * just judged. The server would take the later one, which is not wrong, but it
   * is a vote the user did not cast.
   *
   * The second keystroke is dropped rather than applied to the next card: that
   * card has not been rendered yet, so nobody has seen what they would be
   * judging. A fast "n n" puts one card off, not two.
   */
  const votedRef = useRef<Set<string>>(new Set());
  /** Set by the verdict that empties the deck, honoured once it is stored. */
  const refillWantedRef = useRef(false);

  const mood = settings.mood.trim();
  const queryKey = useMemo(
    () => ["swipe", "deck", settings.mediaType, settings.novelty, mood] as const,
    [settings.mediaType, settings.novelty, mood],
  );

  const query = useQuery<DeckAnswer>({
    queryKey,
    queryFn: ({ signal }) =>
      getDeck(
        {
          media_type: settings.mediaType,
          novelty: settings.novelty,
          ...(mood === "" ? {} : { mood }),
        },
        signal,
      ),
    enabled,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
    // Nothing of this deck survives the page. Which cards have been judged is
    // known here and not in the cache, so a cached answer read again after a
    // detour through another page would put judged cards back on top — and a
    // second verdict on the same title replaces the first rather than being
    // refused. Leaving the page and coming back asks the server instead, which
    // answers with the cards it still considers unvoted.
    gcTime: 0,
    // The only thing that keeps this query running: a `202` that has not timed
    // out. A deck, an error or the deadline all return `false`, and a hidden tab
    // pauses it on its own (`refetchIntervalInBackground` is off by default), so
    // a browser window left open does not spend the user's daily generations.
    refetchInterval: (current) => {
      if (gaveUp || current.state.status === "error") return false;
      const answer = current.state.data;
      return isPendingBatch(answer) ? pollDelay(answer.retryAfterMs, DECK_MIN_POLL_MS) : false;
    },
  });

  const { refetch } = query;
  // `flush` is built once and outlives several renders of the query; it reaches
  // the current `refetch` through this rather than being rebuilt for each one.
  const refetchRef = useRef(refetch);
  useEffect(() => {
    refetchRef.current = refetch;
  }, [refetch]);
  const answer = query.data;
  const deck = isDeck(answer) ? answer : null;
  const waitingNow = isPendingBatch(answer);
  const generating = waitingNow && !gaveUp;

  const remaining = useMemo(
    () => (deck === null ? [] : remainingCards(deck.cards, voted)),
    [deck, voted],
  );

  // --- the deadline on one wait ---------------------------------------------------

  // A `202` being polled, or a first request still in the air: both are "the deck
  // is coming", and a socket that never answers must not be a spinner for ever.
  const awaitingBatch = waitingNow || (query.isFetching && query.data === undefined);
  useEffect(() => {
    if (!awaitingBatch) return undefined;
    const timer = setTimeout(() => {
      setGaveUp(true);
    }, MAX_WAIT_MS);
    return () => {
      clearTimeout(timer);
      // Leaving a wait — a batch arrived, the settings changed, the page closed —
      // is what clears the verdict on it. Nothing here runs during a render.
      setGaveUp(false);
    };
  }, [awaitingBatch, queryKey, attempt]);

  // --- votes --------------------------------------------------------------------

  const apply = useCallback((next: PendingVote[]) => {
    queueRef.current = next;
    setVoteQueue(next);
    setQueue(next);
  }, []);

  const flush = useCallback(() => {
    chainRef.current = chainRef.current
      .then(async () => {
        const batch = queueRef.current;
        if (batch.length === 0) return;
        setSending(true);
        markAttempted(batch.map((entry) => entry.clientVoteId));
        try {
          const outcome = await submitVotes(batch.map(toVoteInput));
          const sent = new Set(batch.map((entry) => entry.clientVoteId));
          apply(queueRef.current.filter((entry) => !sent.has(entry.clientVoteId)));
          setVoteError(null);

          const codes: string[] = [];
          for (const result of outcome.results) {
            const entry = batch.find((item) => item.clientVoteId === result.client_vote_id);
            if (result.outcome === "rejected") {
              codes.push(result.code ?? "unknown");
              continue;
            }
            if (result.request_status != null && entry !== undefined) {
              setAutoRequested({ card: entry.card, status: result.request_status });
            }
          }
          setRejected(codes);

          void queryClient.invalidateQueries({ queryKey: ["swipe", "stats"] });
          void queryClient.invalidateQueries({ queryKey: ["swipe", "likes"] });
          if (outcome.profile_refresh_started === true) {
            void queryClient.invalidateQueries({ queryKey: ["swipe", "profile"] });
          }

          // The deck ran out with this verdict. Asking now, and not when the key
          // was pressed, is what stops the server from answering with the very
          // card it has not been told about yet — which would read as "the deck
          // is empty" a moment before it is true.
          if (refillWantedRef.current) {
            refillWantedRef.current = false;
            setRefilling(false);
            void refetchRef.current();
          }
        } catch (error) {
          // The queue keeps them: same ids, so the resend is a `duplicate`. And
          // nothing is refilled while a verdict is unsent: the server would hand
          // back the card that verdict is about.
          refillWantedRef.current = false;
          setRefilling(false);
          setVoteError(error);
        } finally {
          setSending(false);
        }
      })
      .catch(() => undefined);
  }, [apply, queryClient]);

  const vote = useCallback(
    (value: VoteValue) => {
      const card = remaining[0];
      if (card === undefined || votedRef.current.has(card.id)) return;
      votedRef.current.add(card.id);

      const entry: PendingVote = {
        clientVoteId: newVoteId(),
        vote: value,
        votedAt: new Date().toISOString(),
        card,
      };
      setVoted((current) => new Set(current).add(card.id));
      setLastVote(entry);
      setRejected([]);
      setAutoRequested(null);
      apply([...queueRef.current, entry]);
      if (value === "like" && requestsEnabled && !autoRequest) setRequestPrompt(card);
      if (remaining.length === 1) {
        refillWantedRef.current = true;
        setRefilling(true);
      }
      flush();
    },
    [apply, autoRequest, flush, remaining, requestsEnabled],
  );

  const unvote = useCallback((cardId: string) => {
    votedRef.current.delete(cardId);
    setVoted((current) => {
      const next = new Set(current);
      next.delete(cardId);
      return next;
    });
  }, []);

  const undoMutation = useMutation({
    mutationFn: (entry: PendingVote) => undoVote(entry.card.media_type, entry.card.tmdb_id),
    onSuccess: (_result, entry) => {
      unvote(entry.card.id);
      setLastVote(null);
      void queryClient.invalidateQueries({ queryKey: ["swipe", "stats"] });
      void queryClient.invalidateQueries({ queryKey: ["swipe", "likes"] });
    },
    onError: (error, entry) => {
      // The server no longer has that vote: the card belongs back on the deck all
      // the same, which is the only thing the user asked for.
      if (isApiError(error) && error.status === 404) {
        unvote(entry.card.id);
        setLastVote(null);
      }
    },
  });

  const { mutate: undoOnServer, isPending: undoing } = undoMutation;

  const undo = useCallback(() => {
    const entry = lastVote;
    if (entry === null) return;
    setRequestPrompt(null);
    setAutoRequested(null);
    const stillQueued = queueRef.current.some((item) => item.clientVoteId === entry.clientVoteId);
    if (stillQueued) {
      apply(queueRef.current.filter((item) => item.clientVoteId !== entry.clientVoteId));
      unvote(entry.card.id);
      setLastVote(null);
      setVoteError(null);
      // It is still queued because a submit failed — but a submit can fail on the
      // way back, with the vote already stored. "It never reached the server" is
      // only safe for one that was never sent; for the rest, ask the server to
      // drop it too. `DELETE` is idempotent and its `404` is already handled.
      if (wasAttempted(entry.clientVoteId)) undoOnServer(entry);
      return;
    }
    undoOnServer(entry);
  }, [apply, lastVote, undoOnServer, unvote]);

  const retry = useCallback(() => {
    refillWantedRef.current = false;
    setRefilling(false);
    setGaveUp(false);
    setAttempt((count) => count + 1);
    void refetch();
  }, [refetch]);

  // "The deck is empty" is only true once nothing is in flight: a verdict still
  // being sent, or a refill already asked for, is not an empty deck.
  const stalled =
    deck !== null &&
    remaining.length === 0 &&
    deck.cards.length > 0 &&
    deck.exhausted !== true &&
    !query.isFetching &&
    !sending &&
    queue.length === 0 &&
    !refilling;

  return {
    query,
    remaining,
    current: remaining[0] ?? null,
    next: remaining[1] ?? null,
    generating,
    gaveUp,
    stalled,
    vote,
    undo,
    canUndo: lastVote !== null && !sending && !undoing,
    lastVote,
    sending,
    unsent: queue,
    retryUnsent: flush,
    voteError,
    rejected,
    requestPrompt,
    closeRequestPrompt: () => {
      setRequestPrompt(null);
    },
    autoRequested,
    retry,
  };
}
