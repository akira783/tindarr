import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  decideImportReview,
  deleteImport,
  listImportReview,
  listImports,
  startImport,
  type Import,
  type ImportCandidate,
  type ImportReviewEntry,
} from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { ConfirmDialog } from "../../components/Dialog";
import { Alert, Button, ErrorAlert, Loading, Section } from "../../components/ui";
import { formatDateTime } from "../../lib/format";

const IMPORTS_KEY = ["swipe", "imports"] as const;
/** The failures an import can carry, each with a sentence. Anything else is generic. */
const FAILURES = {
  metadata_unreachable: "imports.failure.metadata_unreachable",
  interrupted: "imports.failure.interrupted",
  internal_error: "imports.failure.internal_error",
} as const;
const FORMATS = {
  netflix: "imports.format.netflix",
  imdb: "imports.format.imdb",
  letterboxd: "imports.format.letterboxd",
} as const;
const STATUSES = {
  running: "imports.status.running",
  complete: "imports.status.complete",
  failed: "imports.status.failed",
} as const;
/** The reasons a parser drops a row, each with a sentence. */
const SKIPPED = {
  supplemental: "imports.skipped.supplemental",
  empty: "imports.skipped.empty",
  not_a_title: "imports.skipped.not_a_title",
  no_id: "imports.skipped.no_id",
  over_limit: "imports.skipped.over_limit",
  rows_over_limit: "imports.skipped.rows_over_limit",
} as const;

function failureKey(code: string): (typeof FAILURES)[keyof typeof FAILURES] | "imports.failure.unknown" {
  return code in FAILURES ? FAILURES[code as keyof typeof FAILURES] : "imports.failure.unknown";
}

function skippedKey(reason: string): (typeof SKIPPED)[keyof typeof SKIPPED] | "imports.skipped.other" {
  return reason in SKIPPED ? SKIPPED[reason as keyof typeof SKIPPED] : "imports.skipped.other";
}

/** While an import is being identified, the list is re-read at this cadence. */
const RUNNING_POLL_MS = 2000;

function reviewKey(importId: string): readonly string[] {
  return ["swipe", "imports", importId, "review"];
}

/** The poster of a candidate, or nothing: a card without one is still a card. */
function Poster({ path, alt }: { path: string | null | undefined; alt: string }): ReactNode {
  const { serverInfo } = useAuth();
  const base = serverInfo?.tmdb_image_base_url;
  if (path == null || base == null) return <span className="poster poster-empty" aria-hidden />;
  return <img className="poster" src={`${base}w154${path}`} alt={alt} loading="lazy" />;
}

function Candidate({
  candidate,
  onAccept,
  busy,
}: {
  candidate: ImportCandidate;
  onAccept: () => void;
  busy: boolean;
}): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const year = candidate.year ?? null;
  return (
    <li className="candidate">
      <Poster path={candidate.poster_path} alt={candidate.title} />
      <div className="flow">
        <p className="candidate-title">
          {candidate.title}
          {year !== null && <span className="hint"> ({year})</span>}
        </p>
        <p className="hint">
          {candidate.media_type === "movie"
            ? t("imports.mediaType.movie")
            : t("imports.mediaType.tv")}
          {" · "}
          {t("imports.similarity", { percent: Math.round(candidate.similarity * 100) })}
        </p>
        <Button variant="primary" disabled={busy} onClick={onAccept}>
          {t("imports.thisOne")}
        </Button>
      </div>
    </li>
  );
}

function ReviewQueue({ importId }: { importId: string }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const key = reviewKey(importId);
  const review = useQuery({
    queryKey: key,
    queryFn: () => listImportReview(importId),
    retry: false,
  });

  const decide = useMutation({
    mutationFn: (input: { entry: ImportReviewEntry; candidate: ImportCandidate | null }) =>
      decideImportReview(
        importId,
        input.entry.id,
        input.candidate === null
          ? { decision: "reject" }
          : {
              decision: "accept",
              title: {
                media_type: input.candidate.media_type,
                tmdb_id: input.candidate.tmdb_id,
              },
            },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: key });
      void queryClient.invalidateQueries({ queryKey: IMPORTS_KEY });
    },
  });

  if (review.isPending) return <Loading />;
  if (review.error !== null) return <ErrorAlert error={review.error} />;
  const entries = review.data.entries;
  if (entries.length === 0) return <p>{t("imports.review.empty")}</p>;

  return (
    <>
      <p>{t("imports.review.help")}</p>
      <ErrorAlert error={decide.error} />
      <ul className="list">
        {entries.map((entry) => (
          <li key={entry.id} className="card">
            <p className="review-query">{entry.query}</p>
            <p className="hint">
              {entry.episodes > 0
                ? t("imports.review.episodes", { count: entry.episodes })
                : t("imports.review.noEpisodes")}
            </p>
            {entry.candidates.length === 0 ? (
              <p>{t("imports.review.nothingFound")}</p>
            ) : (
              <ul className="candidates">
                {entry.candidates.map((candidate) => (
                  <Candidate
                    key={`${candidate.media_type}-${candidate.tmdb_id}`}
                    candidate={candidate}
                    busy={decide.isPending}
                    onAccept={() => {
                      decide.mutate({ entry, candidate });
                    }}
                  />
                ))}
              </ul>
            )}
            <Button
              disabled={decide.isPending}
              onClick={() => {
                decide.mutate({ entry, candidate: null });
              }}
            >
              {t("imports.review.noneOfThese")}
            </Button>
          </li>
        ))}
      </ul>
    </>
  );
}

function ImportCard({
  record,
  onForget,
}: {
  record: Import;
  onForget: () => void;
}): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const [open, setOpen] = useState(false);
  const skipped = Object.entries(record.skipped).filter(([, count]) => count > 0);

  return (
    <li className="card">
      <p className="import-title">
        {t(FORMATS[record.format])}
        <span className={`badge badge-${record.status}`}>{t(STATUSES[record.status])}</span>
      </p>
      <p className="hint">{formatDateTime(record.created_at, i18n.language)}</p>
      {record.status === "failed" && record.error_code != null && (
        <Alert kind="error">{t(failureKey(record.error_code))}</Alert>
      )}
      {record.status === "complete" && (
        <>
          <p>
            {t("imports.counts", {
              titles: record.titles,
              matched: record.matched,
              queued: record.queued,
            })}
          </p>
          {skipped.length > 0 && (
            <p className="hint">
              {skipped.map(([reason, count]) => t(skippedKey(reason), { count })).join(" · ")}
            </p>
          )}
          {record.queued > 0 && (
            <Button
              variant="primary"
              onClick={() => {
                setOpen((value) => !value);
              }}
            >
              {open ? t("imports.review.hide") : t("imports.review.open", { count: record.queued })}
            </Button>
          )}
        </>
      )}
      <Button variant="danger" onClick={onForget}>
        {t("imports.forget")}
      </Button>
      {open && <ReviewQueue importId={record.id} />}
    </li>
  );
}

export function ImportsPage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [confirm, setConfirm] = useState<Import | null>(null);

  const imports = useQuery({
    queryKey: IMPORTS_KEY,
    queryFn: listImports,
    retry: false,
    // An upload answers before its titles are identified, so the list follows it.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((row) => row.status === "running") ? RUNNING_POLL_MS : false,
  });

  const upload = useMutation({
    mutationFn: startImport,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: IMPORTS_KEY });
    },
  });

  const forget = useMutation({
    mutationFn: (record: Import) => deleteImport(record.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: IMPORTS_KEY });
    },
  });

  return (
    <main id="main" className="page">
      <h1>{t("imports.title")}</h1>
      <p>{t("imports.help")}</p>

      <Section title={t("imports.upload.title")}>
        <p>{t("imports.upload.help")}</p>
        <ErrorAlert error={upload.error} />
        {upload.isSuccess && <Alert kind="success">{t("imports.upload.started")}</Alert>}
        <div className="field">
          <label htmlFor="import-file">{t("imports.upload.field")}</label>
          <input
            id="import-file"
            ref={fileRef}
            type="file"
            accept=".csv,.zip,text/csv,application/zip"
            disabled={upload.isPending}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file !== undefined) upload.mutate(file);
              event.target.value = "";
            }}
          />
        </div>
        <p className="hint">{t("imports.upload.privacy")}</p>
      </Section>

      <ErrorAlert error={imports.error ?? forget.error} />
      {imports.isPending ? (
        <Loading />
      ) : (imports.data ?? []).length === 0 ? (
        <p>{t("imports.empty")}</p>
      ) : (
        <ul className="list">
          {(imports.data ?? []).map((record) => (
            <ImportCard
              key={record.id}
              record={record}
              onForget={() => {
                setConfirm(record);
              }}
            />
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={confirm !== null}
        title={t("imports.forget")}
        message={t("imports.forgetConfirm")}
        onCancel={() => {
          setConfirm(null);
        }}
        onConfirm={() => {
          if (confirm !== null) forget.mutate(confirm);
          setConfirm(null);
        }}
      />
    </main>
  );
}
