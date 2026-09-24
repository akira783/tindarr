import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  getCalibrationGrid,
  submitCalibrationGrid,
  type GridTitle,
} from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { Alert, Button, ErrorAlert, Loading } from "../../components/ui";

const MAX_PAGE = 10;

function gridKey(page: number): readonly (string | number)[] {
  return ["swipe", "calibration", page];
}

function refOf(title: GridTitle): string {
  return `${title.media_type}-${title.tmdb_id}`;
}

/**
 * One poster, ticked or not. A button rather than a checkbox: the whole tile is the
 * target, `aria-pressed` says what state it is in, and the label is the title, so a
 * screen reader hears "Inception, seen" and not "checkbox".
 */
function PosterTile({
  title,
  seen,
  onToggle,
}: {
  title: GridTitle;
  seen: boolean;
  onToggle: () => void;
}): ReactNode {
  const { serverInfo } = useAuth();
  const base = serverInfo?.tmdb_image_base_url;
  const label = title.year == null ? title.title : `${title.title} (${title.year})`;
  return (
    <li>
      <button
        type="button"
        className={seen ? "poster-tile poster-tile-on" : "poster-tile"}
        aria-pressed={seen}
        onClick={onToggle}
      >
        {title.poster_path != null && base != null ? (
          <img className="poster" src={`${base}w185${title.poster_path}`} alt="" loading="lazy" />
        ) : (
          <span className="poster poster-empty" aria-hidden />
        )}
        <span className="poster-label">{label}</span>
      </button>
    </li>
  );
}

export function CalibrationPage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const [recorded, setRecorded] = useState<number | null>(null);

  const wall = useQuery({
    queryKey: gridKey(page),
    queryFn: () => getCalibrationGrid(page),
    retry: false,
  });

  const submit = useMutation({
    mutationFn: (titles: GridTitle[]) =>
      submitCalibrationGrid(
        titles.map((title) => ({
          media_type: title.media_type,
          tmdb_id: title.tmdb_id,
          seen: ticked[refOf(title)] === true,
        })),
      ),
    onSuccess: (count) => {
      setRecorded(count);
      setTicked({});
      void queryClient.invalidateQueries({ queryKey: ["swipe", "calibration"] });
      setPage((value) => Math.min(value + 1, MAX_PAGE));
    },
  });

  const titles = wall.data?.titles ?? [];
  const chosen = Object.values(ticked).filter(Boolean).length;

  return (
    <main id="main" className="page">
      <h1>{t("calibration.title")}</h1>
      <p>{t("calibration.help")}</p>
      <ErrorAlert error={wall.error ?? submit.error} />
      {recorded !== null && <Alert kind="success">{t("calibration.recorded", { count: recorded })}</Alert>}

      {wall.isPending ? (
        <Loading />
      ) : titles.length === 0 ? (
        <p>{t("calibration.empty")}</p>
      ) : (
        <>
          <ul className="poster-grid">
            {titles.map((title) => (
              <PosterTile
                key={refOf(title)}
                title={title}
                seen={ticked[refOf(title)] === true}
                onToggle={() => {
                  setTicked((current) => ({
                    ...current,
                    [refOf(title)]: current[refOf(title)] !== true,
                  }));
                }}
              />
            ))}
          </ul>
          <div className="row">
            <Button
              variant="primary"
              disabled={submit.isPending}
              onClick={() => {
                submit.mutate(titles);
              }}
            >
              {t("calibration.submit", { count: chosen })}
            </Button>
            <Button
              disabled={submit.isPending || page >= MAX_PAGE}
              onClick={() => {
                setTicked({});
                setPage((value) => Math.min(value + 1, MAX_PAGE));
              }}
            >
              {t("calibration.more")}
            </Button>
          </div>
        </>
      )}
    </main>
  );
}
