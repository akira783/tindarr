import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode, type SyntheticEvent } from "react";
import { useTranslation } from "react-i18next";

import {
  getSettings,
  updateSettings,
  type PasswordSignIn,
  type ServerSettings,
  type ServerSettingsPatch,
} from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import {
  Alert,
  Button,
  CheckboxField,
  ErrorAlert,
  Loading,
  SelectField,
  TextField,
} from "../../components/ui";

const SETTINGS_KEY = ["admin", "settings"] as const;

interface FormState {
  name: string;
  publicUrl: string;
  passwordSignIn: PasswordSignIn;
  language: string;
  streamingRegion: string;
  dailyGenerationLimit: string;
  warmUpEnabled: boolean;
  excludeAdult: boolean;
  minYear: string;
  excludedGenres: string;
  excludedLanguages: string;
}

function toForm(settings: ServerSettings): FormState {
  return {
    name: settings.name,
    publicUrl: settings.public_url ?? "",
    passwordSignIn: settings.password_sign_in,
    language: settings.language,
    streamingRegion: settings.streaming_region ?? "",
    dailyGenerationLimit: String(settings.daily_generation_limit),
    warmUpEnabled: settings.warm_up_enabled,
    excludeAdult: settings.content_filters.exclude_adult,
    minYear: settings.content_filters.min_year == null ? "" : String(settings.content_filters.min_year),
    excludedGenres: (settings.content_filters.excluded_genres ?? []).join(", "),
    excludedLanguages: (settings.content_filters.excluded_original_languages ?? []).join(", "),
  };
}

function toList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** Only the fields the user actually changed are sent: the patch is all-or-nothing. */
export function buildPatch(current: ServerSettings, form: FormState): ServerSettingsPatch {
  const initial = toForm(current);
  const patch: ServerSettingsPatch = {};
  if (form.name !== initial.name) patch.name = form.name;
  if (form.publicUrl !== initial.publicUrl) patch.public_url = form.publicUrl === "" ? null : form.publicUrl;
  if (form.passwordSignIn !== initial.passwordSignIn) patch.password_sign_in = form.passwordSignIn;
  if (form.language !== initial.language) patch.language = form.language;
  if (form.streamingRegion !== initial.streamingRegion) {
    patch.streaming_region = form.streamingRegion === "" ? null : form.streamingRegion.toUpperCase();
  }
  if (form.dailyGenerationLimit !== initial.dailyGenerationLimit) {
    patch.daily_generation_limit = Number(form.dailyGenerationLimit);
  }
  if (form.warmUpEnabled !== initial.warmUpEnabled) patch.warm_up_enabled = form.warmUpEnabled;
  if (
    form.excludeAdult !== initial.excludeAdult ||
    form.minYear !== initial.minYear ||
    form.excludedGenres !== initial.excludedGenres ||
    form.excludedLanguages !== initial.excludedLanguages
  ) {
    patch.content_filters = {
      exclude_adult: form.excludeAdult,
      min_year: form.minYear === "" ? null : Number(form.minYear),
      excluded_genres: toList(form.excludedGenres),
      excluded_original_languages: toList(form.excludedLanguages),
    };
  }
  return patch;
}

export function SettingsPage(): ReactNode {
  const settings = useQuery({ queryKey: SETTINGS_KEY, queryFn: getSettings, retry: false });

  if (settings.isPending) return <Loading />;
  if (settings.error !== null) return <ErrorAlert error={settings.error} />;
  // Keyed on the saved values, so a save restarts the form from what the server
  // now holds instead of copying server state into a React state in an effect.
  return <SettingsForm key={JSON.stringify(settings.data)} settings={settings.data} />;
}

function SettingsForm({ settings: current }: { settings: ServerSettings }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const { state } = useAuth();
  const mediaServerAdmin = state.status === "web" && state.user.media_server_admin === true;
  const [form, setForm] = useState<FormState>(() => toForm(current));
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (patch: ServerSettingsPatch) => updateSettings(patch),
    onSuccess: (result) => {
      queryClient.setQueryData(SETTINGS_KEY, result);
      setSaved(true);
    },
  });

  const locked = new Set(current.locked_fields ?? []);
  const update = (patch: Partial<FormState>): void => {
    setSaved(false);
    setForm({ ...form, ...patch });
  };

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setSaved(false);
    save.mutate(buildPatch(current, form));
  };

  return (
    <main id="main" className="page">
      <h1>{t("settings.title")}</h1>
      <ErrorAlert error={save.error} />
      {saved && <Alert kind="success">{t("settings.saved")}</Alert>}
      <form onSubmit={submit} className="flow">
        <section className="card">
          <h2>{t("settings.general")}</h2>
          <TextField
            label={t("settings.name")}
            value={form.name}
            maxLength={60}
            disabled={locked.has("name")}
            locked={locked.has("name")}
            lockedNote={t("settings.locked")}
            onChange={(event) => {
              update({ name: event.target.value });
            }}
          />
          <TextField
            label={t("settings.publicUrl")}
            hint={
              mediaServerAdmin ? t("settings.publicUrlHint") : t("settings.mediaServerAdminOnly")
            }
            type="url"
            value={form.publicUrl}
            disabled={locked.has("public_url") || !mediaServerAdmin}
            locked={locked.has("public_url")}
            lockedNote={t("settings.locked")}
            onChange={(event) => {
              update({ publicUrl: event.target.value });
            }}
          />
          <SelectField
            label={t("settings.passwordSignIn")}
            hint={mediaServerAdmin ? undefined : t("settings.mediaServerAdminOnly")}
            value={form.passwordSignIn}
            disabled={locked.has("password_sign_in") || !mediaServerAdmin}
            locked={locked.has("password_sign_in")}
            lockedNote={t("settings.locked")}
            onChange={(event) => {
              update({ passwordSignIn: event.target.value as PasswordSignIn });
            }}
            options={[
              { value: "enabled", label: t("settings.passwordSignInOption.enabled") },
              { value: "lan_only", label: t("settings.passwordSignInOption.lan_only") },
              { value: "disabled", label: t("settings.passwordSignInOption.disabled") },
            ]}
          />
        </section>

        <section className="card">
          <h2>{t("settings.contentFilters")}</h2>
          <p className="hint">{t("settings.later")}</p>
          <TextField
            label={t("settings.language")}
            value={form.language}
            maxLength={16}
            disabled={locked.has("language")}
            onChange={(event) => {
              update({ language: event.target.value });
            }}
          />
          <TextField
            label={t("settings.streamingRegion")}
            hint={t("settings.streamingRegionHint")}
            value={form.streamingRegion}
            maxLength={2}
            disabled={locked.has("streaming_region")}
            onChange={(event) => {
              update({ streamingRegion: event.target.value.toUpperCase() });
            }}
          />
          <TextField
            label={t("settings.dailyGenerationLimit")}
            type="number"
            min={0}
            value={form.dailyGenerationLimit}
            disabled={locked.has("daily_generation_limit")}
            onChange={(event) => {
              update({ dailyGenerationLimit: event.target.value });
            }}
          />
          <CheckboxField
            label={t("settings.warmUpEnabled")}
            checked={form.warmUpEnabled}
            disabled={locked.has("warm_up_enabled")}
            onChange={(event) => {
              update({ warmUpEnabled: event.target.checked });
            }}
          />
          <CheckboxField
            label={t("settings.excludeAdult")}
            checked={form.excludeAdult}
            onChange={(event) => {
              update({ excludeAdult: event.target.checked });
            }}
          />
          <TextField
            label={t("settings.minYear")}
            type="number"
            value={form.minYear}
            onChange={(event) => {
              update({ minYear: event.target.value });
            }}
          />
          <TextField
            label={t("settings.excludedGenres")}
            hint={t("settings.listHint")}
            value={form.excludedGenres}
            onChange={(event) => {
              update({ excludedGenres: event.target.value });
            }}
          />
          <TextField
            label={t("settings.excludedLanguages")}
            hint={t("settings.listHint")}
            value={form.excludedLanguages}
            onChange={(event) => {
              update({ excludedLanguages: event.target.value });
            }}
          />
        </section>

        <Button type="submit" variant="primary" disabled={save.isPending}>
          {t("common:actions.save")}
        </Button>
      </form>
    </main>
  );
}
