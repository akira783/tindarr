/**
 * The deck's keyboard, decided in one pure function (roadmap 4.6).
 *
 * Arrow keys for the four verdicts — the same four directions the app will swipe
 * in (roadmap step 7), so the two clients teach the same thing — plus three
 * letters for what has no direction left.
 *
 * The keys are drawn as glyphs (`KEY_CAPS`) rather than named in the catalogues:
 * a key is the same shape in every language, and the name a screen reader needs
 * is the specification's, which `SHORTCUTS` gives to `aria-keyshortcuts`.
 */
import type { VoteValue } from "../../api/operations";

export type DeckAction = VoteValue | "undo" | "trailer";

const BY_KEY: Readonly<Record<string, DeckAction>> = {
  ArrowRight: "like",
  ArrowLeft: "dislike",
  ArrowUp: "seen_liked",
  ArrowDown: "seen_disliked",
  n: "skip",
  u: "undo",
  t: "trailer",
};

/**
 * The five verdicts, in the order the buttons show them.
 *
 * The order is the arrows': refusal on the left, wanting it on the right, "not
 * now" in the middle, and the two "seen it" verdicts where their arrows are
 * (design/Tindarr Refonte.dc.html, "Barre de verdicts"). A hand that has learnt
 * the keyboard then finds the buttons where the keys are.
 */
export const VERDICTS: readonly VoteValue[] = [
  "dislike",
  "seen_disliked",
  "skip",
  "seen_liked",
  "like",
];

/** Catalogue suffix for a verdict: `deck.verdict.<name>`. */
export type VerdictKey = "like" | "dislike" | "seenLiked" | "seenDisliked" | "skip";

export const VERDICT_KEYS: Readonly<Record<VoteValue, VerdictKey>> = {
  like: "like",
  dislike: "dislike",
  seen_liked: "seenLiked",
  seen_disliked: "seenDisliked",
  skip: "skip",
};

/**
 * What a key looks like on a button.
 *
 * The glyph is drawn, `aria-hidden`, beside the verdict; the key's real name goes
 * to `aria-keyshortcuts` below, which is where a screen reader looks for it. That
 * way the button's accessible name stays the verdict and nothing reads out
 * "Seen it, liked it Up arrow".
 */
export const KEY_CAPS: Readonly<Record<DeckAction, string>> = {
  like: "\u2192",
  dislike: "\u2190",
  seen_liked: "\u2191",
  seen_disliked: "\u2193",
  skip: "N",
  undo: "U",
  trailer: "T",
};

/** `aria-keyshortcuts` values, which name keys the way the specification does. */
export const SHORTCUTS: Readonly<Record<DeckAction, string>> = {
  like: "ArrowRight",
  dislike: "ArrowLeft",
  seen_liked: "ArrowUp",
  seen_disliked: "ArrowDown",
  skip: "N",
  undo: "U",
  trailer: "T",
};

interface KeyLike {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  target?: EventTarget | null;
  isComposing?: boolean;
}

/** True when the keystroke belongs to whatever the user is typing in. */
export function isTypingTarget(target: EventTarget | null | undefined): boolean {
  if (target === null || target === undefined || !(target instanceof Element)) return false;
  const name = target.localName;
  if (name === "input" || name === "textarea" || name === "select" || name === "option") return true;
  return target.closest("[contenteditable]:not([contenteditable=false])") !== null;
}

/**
 * The action a keystroke means, or null when it means nothing here.
 *
 * A modifier turns it back into the browser's own shortcut, a field takes it for
 * itself, and a composition in progress is somebody writing a mood in Japanese.
 */
export function actionForKey(event: KeyLike): DeckAction | null {
  if (event.ctrlKey === true || event.metaKey === true || event.altKey === true) return null;
  if (event.isComposing === true) return null;
  if (isTypingTarget(event.target)) return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  return BY_KEY[key] ?? null;
}
