import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Card } from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { Button } from "../../components/ui";
import { posterUrl, trailerEmbedUrl } from "../../lib/url";
import { lengthOf, sortedProviders } from "./deck-state";
import { KEY_CAPS, SHORTCUTS } from "./keys";

/** "1 h 52", "104 min", "3 seasons", or nothing at all. */
function useLength(card: Card): string | null {
  const { t } = useTranslation(["console", "common"]);
  const length = lengthOf(card);
  if (length === null) return null;
  if ("seasons" in length) return t("deck.card.seasons", { count: length.seasons });
  const hours = Math.floor(length.minutes / 60);
  const minutes = length.minutes % 60;
  return hours === 0
    ? t("deck.card.runtimeShort", { minutes })
    : t("deck.card.runtimeLong", { hours, minutes: String(minutes).padStart(2, "0") });
}

/**
 * The pick type, said three times over: a shape, a word, and a colour.
 *
 * A bet is the one thing that makes this deck different from a catalogue, so it
 * is the one that is also ringed — here and around the poster. Nothing here is
 * carried by colour alone (design/brief.md, "Constraints").
 */
function PickType({ card }: { card: Card }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  return (
    <span className={`pick pick-${card.pick_type}`}>
      <span className="pick-mark" aria-hidden />
      {t(`deck.card.pickType.${card.pick_type}`)}
    </span>
  );
}

function Ratings({ card }: { card: Card }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const ratings = card.ratings ?? {};
  // One text node per value, on purpose: a scale set in its own smaller <span>
  // would split "8.4/10" across elements, and a reader — human or test — looking
  // for the rating as it is written would no longer find it.
  const rows: { label: string; value: string }[] = [];
  if (ratings.tmdb != null) rows.push({ label: t("deck.card.tmdb"), value: `${ratings.tmdb.toFixed(1)}/10` });
  if (ratings.imdb != null) rows.push({ label: t("deck.card.imdb"), value: `${ratings.imdb.toFixed(1)}/10` });
  if (ratings.rotten_tomatoes != null)
    rows.push({ label: t("deck.card.rottenTomatoes"), value: `${ratings.rotten_tomatoes} %` });
  if (ratings.metacritic != null)
    rows.push({ label: t("deck.card.metacritic"), value: `${ratings.metacritic}/100` });

  if (rows.length === 0) return <p className="hint">{t("deck.card.noRatings")}</p>;
  return (
    <dl className="ratings">
      {rows.map((row) => (
        <div className="rating" key={row.label}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Providers({ card }: { card: Card }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const providers = sortedProviders(card);
  if (providers.length === 0) return <p className="hint">{t("deck.card.noProviders")}</p>;
  return (
    <ul className="provider-list">
      {providers.map((provider) => (
        <li
          key={`${provider.provider_id}-${provider.offer}`}
          className={provider.subscribed ? "provider provider-mine" : "provider"}
        >
          <span className="provider-name">{provider.name}</span>
          <span className="hint">{t(`deck.card.offer.${provider.offer}`)}</span>
          {/* Never colour alone: "on your services" is written out. */}
          {provider.subscribed && <span className="badge badge-mine">{t("deck.card.onYourServices")}</span>}
        </li>
      ))}
    </ul>
  );
}

/**
 * The trailer, framed and only on request.
 *
 * Nothing of YouTube is fetched while somebody is merely swiping: the frame is
 * built by the click, on the no-cookie host, and the `src` is rebuilt from a key
 * that had to match the contract's pattern (see `lib/url.ts`). `sandbox` leaves
 * the player its scripts and its own origin and takes everything else, and the
 * console's CSP allows exactly this one origin to be framed (ADR 0009).
 */
/*
 * `allow-scripts` together with `allow-same-origin` is flagged by default, because
 * a *same-origin* frame can then take its own sandbox off. This frame is
 * cross-origin and the CSP pins it to that one host, so "same origin" here means
 * YouTube's and never the console's — the attribute is strictly more restrictive
 * than the alternative, which is no sandbox at all.
 */
/* eslint-disable @eslint-react/dom-no-unsafe-iframe-sandbox */
function Trailer({ card, open, onToggle }: { card: Card; open: boolean; onToggle: () => void }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const src = trailerEmbedUrl(card.trailer);
  if (src === null) return null;
  const label = t("deck.card.trailerTitle", { title: card.title });
  return (
    <div className="trailer">
      <Button onClick={onToggle} aria-expanded={open} aria-keyshortcuts={SHORTCUTS.trailer}>
        {open ? t("deck.card.closeTrailer") : t("deck.card.trailer")}
        <span className="kbd" aria-hidden>
          {KEY_CAPS.trailer}
        </span>
      </Button>
      {open ? (
        <iframe
          className="trailer-frame"
          src={src}
          title={label}
          sandbox="allow-scripts allow-same-origin allow-presentation"
          allow="encrypted-media; fullscreen; picture-in-picture"
          referrerPolicy="no-referrer"
          loading="lazy"
        />
      ) : (
        <p className="hint">{t("deck.card.trailerNote")}</p>
      )}
    </div>
  );
}

/* eslint-enable @eslint-react/dom-no-unsafe-iframe-sandbox */

export interface DeckCardProps {
  card: Card;
  position: number;
  total: number;
  trailerOpen: boolean;
  onToggleTrailer: () => void;
}

/**
 * One card: the poster, and beside it the reason to watch it.
 *
 * The order is the design's (design/Tindarr Refonte.dc.html, "La carte"): what
 * kind of pick it is, the title, the facts in one line, then the rationale —
 * before the summary, set as a quotation, because it is the sentence that has to
 * be read rather than skipped — and last a band of what is known about the title.
 *
 * Everything on it is text React escapes: the rationale is written by a model and
 * the title by TMDb, and neither is ever handed to a markup sink.
 */
export function DeckCard({
  card,
  position,
  total,
  trailerOpen,
  onToggleTrailer,
}: DeckCardProps): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const { serverInfo } = useAuth();
  const poster = posterUrl(serverInfo?.tmdb_image_base_url, card.poster_path, "w342");
  // The poster carries the screen: the backdrop is a wash behind the card, faded
  // into the page, decorative and never the only thing saying anything.
  const backdrop = posterUrl(serverInfo?.tmdb_image_base_url, card.backdrop_path, "w1280");
  const length = useLength(card);
  const kind = t(`deck.card.${card.media_type}`);
  const genres = card.genres ?? [];
  const original =
    card.original_title != null && card.original_title !== "" && card.original_title !== card.title
      ? card.original_title
      : null;

  return (
    <article
      className={`deck-card deck-card--${card.pick_type}`}
      aria-labelledby="deck-card-title"
    >
      {backdrop === null ? null : (
        <div className="deck-backdrop" aria-hidden>
          <img className="deck-backdrop-image" src={backdrop} alt="" />
        </div>
      )}

      <div className="deck-card-poster">
        {poster === null ? (
          <span className="poster poster-empty" aria-hidden />
        ) : (
          // Decorative: the title right next to it is what a screen reader reads.
          <img className="poster" src={poster} alt="" width={342} height={513} />
        )}
      </div>

      <div className="deck-card-body">
        <div className="deck-card-head">
          <PickType card={card} />
          <span className="hint">{t("deck.card.position", { position, total })}</span>
        </div>

        <h2 id="deck-card-title">
          {card.title}
          {card.year == null ? null : <span className="deck-card-year"> ({card.year})</span>}
        </h2>
        {original === null ? null : <p className="deck-card-original">{original}</p>}
        <p className="hint deck-card-facts">
          <span>{kind}</span>
          {length === null ? null : <span> · {length}</span>}
          {genres.length === 0 ? null : <span> · {genres.join(", ")}</span>}
        </p>

        <figure className="rationale-figure">
          <figcaption className="rationale-label">{t("deck.card.why")}</figcaption>
          <blockquote className="rationale">
            {card.rationale == null || card.rationale === ""
              ? t("deck.card.noRationale")
              : card.rationale}
          </blockquote>
        </figure>

        {card.overview == null || card.overview === "" ? null : (
          <p className="overview">{card.overview}</p>
        )}

        <div className="deck-card-band">
          <h3 className="visually-hidden">{t("deck.card.ratings")}</h3>
          <Ratings card={card} />
          {card.availability !== "none" && (
            <span className={`avail avail-${card.availability}`}>
              <span className="avail-mark" aria-hidden />
              {t(`deck.card.availability.${card.availability}`)}
            </span>
          )}
          <h3 className="visually-hidden">{t("deck.card.providers")}</h3>
          <Providers card={card} />
        </div>

        <Trailer card={card} open={trailerOpen} onToggle={onToggleTrailer} />
      </div>
    </article>
  );
}
