import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Calibration, MediaFilter, Novelty } from "../../api/operations";
import { Button, SelectField, TextField } from "../../components/ui";

const MEDIA: readonly MediaFilter[] = ["both", "movie", "tv"];
const NOVELTY: readonly Novelty[] = ["familiar", "balanced", "bold"];

export interface DeckControlsProps {
  mediaType: MediaFilter;
  novelty: Novelty;
  mood: string;
  busy: boolean;
  calibration: Calibration | null;
  onMediaType: (value: MediaFilter) => void;
  onNovelty: (value: Novelty) => void;
  onMood: (value: string) => void;
}

/**
 * What the user steers: media type and novelty (both stored as preferences, so
 * the phone gets the same deck), and a mood that lives only in this session's
 * query string. The mood is applied on submit rather than on every keystroke —
 * each change is a new batch, and a batch costs a generation.
 */
export function DeckControls({
  mediaType,
  novelty,
  mood,
  busy,
  calibration,
  onMediaType,
  onNovelty,
  onMood,
}: DeckControlsProps): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  // The applied mood only ever changes because this form asked for it, so there is
  // nothing to synchronise back: the two are set together.
  const [draft, setDraft] = useState(mood);

  return (
    <section className="card deck-controls" aria-label={t("deck.controls.title")}>
      <div className="row">
        <SelectField
          label={t("deck.controls.mediaType")}
          value={mediaType}
          disabled={busy}
          onChange={(event) => {
            onMediaType(event.target.value as MediaFilter);
          }}
          options={MEDIA.map((value) => ({ value, label: t(`deck.controls.media.${value}`) }))}
        />
        <SelectField
          label={t("deck.controls.novelty")}
          value={novelty}
          disabled={busy}
          onChange={(event) => {
            onNovelty(event.target.value as Novelty);
          }}
          options={NOVELTY.map((value) => ({
            value,
            label: t(`deck.controls.noveltyLevel.${value}`),
          }))}
        />
      </div>

      <form
        className="row deck-mood"
        onSubmit={(event) => {
          event.preventDefault();
          onMood(draft.trim());
        }}
      >
        <TextField
          label={t("deck.controls.mood")}
          hint={t("deck.controls.moodHint")}
          type="text"
          value={draft}
          maxLength={200}
          disabled={busy}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <Button type="submit" disabled={busy || draft.trim() === mood}>
          {t("deck.controls.moodApply")}
        </Button>
        {mood !== "" && (
          <Button
            disabled={busy}
            onClick={() => {
              setDraft("");
              onMood("");
            }}
          >
            {t("deck.controls.moodClear")}
          </Button>
        )}
      </form>

      {calibration !== null && !calibration.complete && (
        <div className="calibration-progress">
          <h3>{t("deck.controls.calibration")}</h3>
          {/* A real <progress>: the browser gives it a role, a value and a label. */}
          <progress
            value={calibration.done}
            max={calibration.target}
            aria-label={t("deck.controls.calibration")}
          >
            {t("deck.controls.calibrationProgress", {
              done: calibration.done,
              target: calibration.target,
            })}
          </progress>
          <p className="hint">
            {t("deck.controls.calibrationProgress", {
              done: calibration.done,
              target: calibration.target,
            })}
          </p>
          <p className="hint">{t("deck.controls.calibrationHint")}</p>
        </div>
      )}
    </section>
  );
}
