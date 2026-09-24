import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Card } from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { Button } from "../../components/ui";
import { posterUrl, trailerEmbedUrl } from "../../lib/url";
import { lengthOf, sortedProviders } from "./deck-state";

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

function Ratings({ card }: { card: Card }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const ratings = card.ratings ?? {};
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
      <Button onClick={onToggle} aria-expanded={open} aria-keyshortcuts="T">
        {open ? t("deck.card.closeTrailer") : t("deck.card.trailer")}
      </Button>
      {open ? (
        <iframe
          className="trailer-frame"
          src={src}
          title={label}
          sandbox="allow-scripts allow-same-origin allow-presentation"
          allow="encrypted-media; fullscreen; picture-in-picture"
          referrerPolicy="strict-origin-when-cross-origin"
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
 * One card. Everything on it is text React escapes: the rationale is written by
 * a model and the title by TMDb, and neither is ever handed to a markup sink.
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
  const length = useLength(card);
  const kind = t(`deck.card.${card.media_type}`);
  const genres = card.genres ?? [];

  return (
    <article className="deck-card" aria-labelledby="deck-card-title">
      <div className="deck-card-poster">
        {poster === null ? (
          <span className="poster poster-empty" aria-hidden />
        ) : (
          // Decorative: the title right next to it is what a screen reader reads.
          <img className="poster" src={poster} alt="" width={342} height={513} />
        )}
      </div>

      <div className="deck-card-body">
        <p className="hint">{t("deck.card.position", { position, total })}</p>
        <h2 id="deck-card-title">
          {card.title}
          {card.year == null ? null : <span className="deck-card-year"> ({card.year})</span>}
        </h2>
        <p className="hint deck-card-facts">
          <span>{kind}</span>
          {length === null ? null : <span> · {length}</span>}
          {genres.length === 0 ? null : <span> · {genres.join(", ")}</span>}
        </p>

        <p className="row">
          <span className="badge badge-pick">{t(`deck.card.pickType.${card.pick_type}`)}</span>
          {card.availability !== "none" && (
            <span className="badge badge-availability">
              {t(`deck.card.availability.${card.availability}`)}
            </span>
          )}
        </p>

        <h3>{t("deck.card.why")}</h3>
        <p className="rationale">
          {card.rationale == null || card.rationale === ""
            ? t("deck.card.noRationale")
            : card.rationale}
        </p>

        {card.overview == null || card.overview === "" ? null : (
          <p className="overview">{card.overview}</p>
        )}

        <h3>{t("deck.card.ratings")}</h3>
        <Ratings card={card} />

        <h3>{t("deck.card.providers")}</h3>
        <Providers card={card} />

        <Trailer card={card} open={trailerOpen} onToggle={onToggleTrailer} />
      </div>
    </article>
  );
}
