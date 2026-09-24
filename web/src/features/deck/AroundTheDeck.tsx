import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  getProfile,
  getStats,
  listLikes,
  refreshProfile,
  requestTitle,
  saveProfile,
  type Like,
} from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { Alert, Button, ErrorAlert, Loading } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { posterUrl, safeHttpUrl } from "../../lib/url";

/**
 * A section that costs nothing until it is opened.
 *
 * Every panel here is a side call, and the rule for all of them is the same: the
 * deck never waits on one. A closed `<details>` runs no query at all, and an open
 * one renders its own spinner beside a deck that keeps working.
 */
function Panel({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  children: ReactNode;
}): ReactNode {
  return (
    <details
      className="card panel"
      open={open}
      onToggle={(event) => {
        onToggle(event.currentTarget.open);
      }}
    >
      <summary>
        <h2>{title}</h2>
      </summary>
      {open ? children : null}
    </details>
  );
}

type LikeFilter = "all" | "to_request" | "requested";

/** Pick types the catalogues name; anything else the server adds later reads as itself. */
const PICK_TYPES = ["safe", "explore", "calibration"] as const;

function pickTypeLabel(
  t: (key: `deck.card.pickType.${(typeof PICK_TYPES)[number]}`) => string,
  pick: string,
): string {
  const known = PICK_TYPES.find((value) => value === pick);
  return known === undefined ? pick : t(`deck.card.pickType.${known}`);
}

function LikeRow({ like }: { like: Like }): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const { serverInfo } = useAuth();
  const queryClient = useQueryClient();
  const poster = posterUrl(serverInfo?.tmdb_image_base_url, like.poster_path, "w185");
  const watch = safeHttpUrl(like.watch_url);

  const ask = useMutation({
    mutationFn: () => requestTitle({ media_type: like.media_type, tmdb_id: like.tmdb_id }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["swipe", "likes"] });
    },
  });

  return (
    <li className="like">
      {poster === null ? (
        <span className="poster poster-empty" aria-hidden />
      ) : (
        <img className="poster" src={poster} alt="" width={92} height={138} loading="lazy" />
      )}
      <div className="like-body">
        <p className="like-title">
          {like.title}
          {like.year == null ? null : ` (${like.year})`}
        </p>
        <p className="hint">{formatDateTime(like.liked_at, i18n.language)}</p>
        <div className="row">
          {like.requested ? (
            <span className="badge">{t("deck.likes.requested")}</span>
          ) : (
            <Button
              disabled={ask.isPending}
              onClick={() => {
                ask.mutate();
              }}
            >
              {t("deck.likes.request")}
            </Button>
          )}
          {like.availability !== "none" && (
            <span className="badge">{t(`deck.card.availability.${like.availability}`)}</span>
          )}
          {watch !== null && (
            <a href={watch} rel="noreferrer noopener" target="_blank">
              {t("deck.likes.watch")}
            </a>
          )}
        </div>
        {ask.isSuccess && <Alert kind="success">{t(`deck.request.status.${ask.data}`)}</Alert>}
        <ErrorAlert error={ask.error} />
      </div>
    </li>
  );
}

function Likes(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [filter, setFilter] = useState<LikeFilter>("all");
  const likes = useQuery({
    queryKey: ["swipe", "likes", filter] as const,
    queryFn: () => listLikes(filter),
    retry: false,
  });

  return (
    <>
      <p className="hint">{t("deck.likes.help")}</p>
      <div className="tabs" role="group" aria-label={t("deck.likes.filter")}>
        {(["all", "to_request", "requested"] as const).map((value) => (
          <button
            key={value}
            type="button"
            className={filter === value ? "tab tab-active" : "tab"}
            aria-pressed={filter === value}
            onClick={() => {
              setFilter(value);
            }}
          >
            {t(
              value === "all"
                ? "deck.likes.filterAll"
                : value === "to_request"
                  ? "deck.likes.filterToRequest"
                  : "deck.likes.filterRequested",
            )}
          </button>
        ))}
      </div>
      <ErrorAlert error={likes.error} />
      {likes.isPending ? (
        <Loading />
      ) : (likes.data?.likes.length ?? 0) === 0 ? (
        <p>{t("deck.likes.empty")}</p>
      ) : (
        <ul className="list like-list">
          {likes.data?.likes.map((like) => (
            <LikeRow key={`${like.media_type}-${like.tmdb_id}`} like={like} />
          ))}
        </ul>
      )}
    </>
  );
}

function Taste(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);

  const profile = useQuery({
    queryKey: ["swipe", "profile"] as const,
    queryFn: getProfile,
    retry: false,
    // A rewrite runs in the background; the contract says to poll this until it
    // is over, and nothing else here keeps a timer alive.
    refetchInterval: (query) => (query.state.data?.refreshing === true ? 3000 : false),
  });

  const save = useMutation({
    mutationFn: (text: string) => saveProfile(text),
    onSuccess: (state) => {
      queryClient.setQueryData(["swipe", "profile"], state);
      setDraft(null);
    },
  });

  const refresh = useMutation({
    mutationFn: refreshProfile,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["swipe", "profile"] });
    },
  });

  const state = profile.data;
  const text = state?.profile?.text ?? "";

  if (profile.isPending) return <Loading />;

  return (
    <>
      <p className="hint">{t("deck.profile.help")}</p>
      <ErrorAlert error={profile.error ?? save.error ?? refresh.error} />
      {state?.refresh_error != null && (
        <Alert kind="warning">{t("deck.profile.refreshError", { code: state.refresh_error })}</Alert>
      )}
      {state?.refreshing === true && <Alert>{t("deck.profile.refreshing")}</Alert>}

      {draft === null ? (
        <>
          {text === "" ? <p>{t("deck.profile.empty")}</p> : <p className="profile-text">{text}</p>}
          {state?.profile != null && (
            <p className="hint">
              {state.profile.user_edited ? `${t("deck.profile.userEdited")} · ` : ""}
              {t("deck.profile.updated", {
                when: formatDateTime(state.profile.updated_at, i18n.language),
              })}
              {" · "}
              {t("deck.profile.votesSince", { count: state.profile.votes_since_update })}
            </p>
          )}
          <div className="row">
            <Button
              onClick={() => {
                setDraft(text);
              }}
            >
              {t("deck.profile.edit")}
            </Button>
            <Button
              disabled={refresh.isPending || state?.refreshing === true}
              onClick={() => {
                refresh.mutate();
              }}
            >
              {t("deck.profile.refresh")}
            </Button>
          </div>
        </>
      ) : (
        <form
          className="flow"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(draft);
          }}
        >
          <label className="field">
            <span>{t("deck.profile.title")}</span>
            <textarea
              rows={8}
              maxLength={4000}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
              }}
            />
          </label>
          <div className="row">
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {t("common:actions.save")}
            </Button>
            <Button
              onClick={() => {
                setDraft(null);
              }}
            >
              {t("common:actions.cancel")}
            </Button>
          </div>
        </form>
      )}
    </>
  );
}

function Numbers(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const stats = useQuery({ queryKey: ["swipe", "stats"] as const, queryFn: getStats, retry: false });
  const percent = new Intl.NumberFormat(i18n.language, { style: "percent", maximumFractionDigits: 0 });

  if (stats.isPending) return <Loading />;
  const data = stats.data;
  if (data === undefined) return <ErrorAlert error={stats.error} />;
  if (data.total === 0 && data.skips === 0) return <p>{t("deck.stats.empty")}</p>;

  const cells: { label: string; value: string }[] = [
    { label: t("deck.stats.total"), value: String(data.total) },
    { label: t("deck.stats.likes"), value: String(data.likes) },
    { label: t("deck.stats.dislikes"), value: String(data.dislikes) },
    { label: t("deck.stats.seenLiked"), value: String(data.seen_liked) },
    { label: t("deck.stats.seenDisliked"), value: String(data.seen_disliked) },
    { label: t("deck.stats.skips"), value: String(data.skips) },
    { label: t("deck.stats.requested"), value: String(data.requested) },
    { label: t("deck.stats.likeRate"), value: percent.format(data.like_rate) },
    { label: t("deck.stats.requestRate"), value: percent.format(data.request_rate) },
  ];

  return (
    <>
      <p className="hint">{t("deck.stats.help")}</p>
      <dl className="stat-row">
        {cells.map((cell) => (
          <div className="stat" key={cell.label}>
            <dt>{cell.label}</dt>
            <dd>{cell.value}</dd>
          </div>
        ))}
      </dl>
      <h3>{t("deck.stats.byPickType")}</h3>
      <ul className="list">
        {Object.entries(data.by_pick_type).map(([pick, counts]) => (
          <li key={pick}>
            {pickTypeLabel(t, pick)} —{" "}
            {t("deck.stats.pickTypeTotal", { likes: counts.likes, total: counts.total })}
          </li>
        ))}
      </ul>
    </>
  );
}

/** "My likes", the taste profile and this account's numbers, below the deck. */
export function AroundTheDeck(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [open, setOpen] = useState({ likes: false, taste: false, numbers: false });

  return (
    <div className="around-the-deck">
      <Panel
        title={t("deck.likes.title")}
        open={open.likes}
        onToggle={(value) => {
          setOpen((current) => ({ ...current, likes: value }));
        }}
      >
        <Likes />
      </Panel>
      <Panel
        title={t("deck.profile.title")}
        open={open.taste}
        onToggle={(value) => {
          setOpen((current) => ({ ...current, taste: value }));
        }}
      >
        <Taste />
      </Panel>
      <Panel
        title={t("deck.stats.title")}
        open={open.numbers}
        onToggle={(value) => {
          setOpen((current) => ({ ...current, numbers: value }));
        }}
      >
        <Numbers />
      </Panel>
    </div>
  );
}
