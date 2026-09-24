# Tindarr — design brief

Paste this into Claude Design. Everything below describes a product that exists and runs;
the screens are real, the data is real, and the result will be implemented in code.

## What it is

Tindarr is Tinder for films and series, self-hosted. It shows one AI-picked card at a
time; you swipe right and the title goes straight to your request queue, left and it is
gone. It runs on your own server, next to Jellyfin, Emby or Plex.

Two clients share one visual language:

- **today, a web console** (React, served by the server, used on a laptop but must work at
  phone width),
- **later, an Android app** (Expo/React Native) — so the outcome must be **tokens and
  component specs**, not one beautiful page.

The audience is people who already run a media server at home. They are not impressed by
chrome; they want the poster, the reason, and one keystroke.

## Direction

**Dark and cinematic.** Deep background, posters carrying the screen, warm accents, the
interface stepping back. Letterboxd and the streaming apps are the neighbourhood, not
Tinder's gradients — but the swipe gesture should still feel physical and a little
playful. Light mode must exist and be good, not an afterthought.

## The screens, in order of importance

### 1. The deck (the product)

One card at a time, and nothing else competing for attention. The card carries, all of it
real data from the API:

| Field | Example |
|---|---|
| `poster_path`, `backdrop_path` | TMDb images |
| `title`, `original_title`, `year` | "Le Seigneur des anneaux", 2001 |
| `media_type` | film or series |
| `runtime_minutes` **or** `seasons` | 178 min / 3 seasons |
| `genres` | Aventure, Fantastique |
| `overview` | a paragraph |
| `rationale` | **one sentence written by the AI explaining why you, specifically** |
| `ratings` | TMDb, and sometimes IMDb / Rotten Tomatoes / Metacritic |
| `providers` | Netflix, Canal+… each flagged `subscribed` when the user pays for it |
| `trailer` | a YouTube key |
| `availability` | none / requested / processing / partially available / available |
| `pick_type` | `safe`, `explore` (a bet), or `calibration` |

Five verdicts, keyboard first, buttons for the mouse:

- like, dislike
- "seen it, liked it", "seen it, not for me" — these are frequent, roughly half the cards
- "not now" (skip)

Plus: undo the last verdict, media-type and novelty switches (familiar / balanced /
surprise me), an optional free-text mood, and calibration progress for the first 15 votes.

**The hard part to solve visually:** `rationale` and `pick_type` are what make this
product different from a catalogue, and today they are invisible. A bet should look like a
bet. The reason should be read, not skipped.

### 2. The empty and waiting states (they are frequent, not edge cases)

A batch takes 15–25 seconds to generate, so the wait is the first thing a new user sees.
Then: daily generation cap reached, metadata service unreachable, AI provider failing, no
AI provider configured yet, deck exhausted. Each needs a sentence that says what to do
next. **Design the wait as a first-class screen, not a spinner.**

### 3. The console shell

Navigation between deck, likes, taste profile, connectors, users, settings, "connect a
phone" (a QR code). Sign-in page. It is administration, and it should feel calm next to
the deck's warmth.

### 4. Around the deck

- **My likes**: to request / already requested, each with a request button.
- **Taste profile**: short bullets, "Loves / Avoids / Nuances", editable by the user.
- **Stats**: like rate, share of already-seen cards, per pick type.

## Constraints that are not negotiable

- **No inline styles and no inline scripts** — the console runs under a strict
  Content-Security-Policy. Everything must be expressible as CSS classes and tokens.
- **Keyboard first**: every verdict and control reachable and visible on focus. The focus
  ring is part of the design, not something to hide.
- **Contrast**: WCAG AA at least, in both themes. Provider badges and pick-type marks must
  not rely on colour alone.
- **Phone width in a browser**, no horizontal scrolling, 16px side gutters.
- Posters come from TMDb at fixed aspect ratios; some titles have no poster at all.

## What to hand back

1. **Design tokens**: colour (both themes), typography scale, spacing, radii, elevation,
   motion durations — as plain CSS custom properties, replacing the ones below.
2. **Component specs** for: the card, the verdict bar, the badges (provider, pick type,
   availability, rating), the waiting state, the empty states, the shell and its
   navigation, the forms.
3. **Artboards**: the deck at desktop and phone width, in dark and light, plus one waiting
   state and one empty state.

## The tokens it replaces (current, unstyled)

```css
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --surface: #ffffff;
  --text: #16181d;
  --muted: #5a6070;
  --border: #d9dde5;
  --accent: #2f6df6;
  --accent-text: #ffffff;
  --danger: #b3261e;
  --success: #14663f;
  --warning: #8a5a00;
  --radius: 10px;
  --space: 1rem;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
}
```

The whole stylesheet is 752 lines written while building features, with no design pass:
assume nothing in it is worth keeping except the structure it proves (cards, forms,
alerts, badges, dialogs).
