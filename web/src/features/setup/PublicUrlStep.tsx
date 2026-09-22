import { useState, type SyntheticEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { updateSettings } from "../../api/operations";
import { Alert, Button, ErrorAlert, TextField } from "../../components/ui";
import { currentOrigin } from "../../lib/url";

/**
 * Last step of the wizard: the address phones will reach the server at. The
 * server verifies it before saving (docs/auth.md, section 10), so a failure
 * comes back as `public_url_unverified` with a coarse reason.
 */
export function PublicUrlStep({ onFinish }: { onFinish: () => void }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const origin = currentOrigin();
  const [value, setValue] = useState(origin);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    updateSettings({ public_url: value })
      .then(() => {
        onFinish();
      })
      .catch((caught: unknown) => {
        setError(caught);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className="card">
      <h2>{t("setup.publicUrl.title")}</h2>
      <Alert kind="success">{t("setup.publicUrl.done")}</Alert>
      <p>{t("setup.publicUrl.help")}</p>
      <ErrorAlert error={error} />
      {busy && <Alert kind="info">{t("setup.publicUrl.checking")}</Alert>}
      <form onSubmit={submit} className="flow">
        <TextField
          label={t("setup.publicUrl.label")}
          type="url"
          required
          value={value}
          hint={t("setup.publicUrl.suggestion", { origin })}
          onChange={(event) => {
            setValue(event.target.value);
          }}
        />
        <div className="row">
          <Button type="submit" variant="primary" disabled={busy}>
            {t("common:actions.save")}
          </Button>
          <Button onClick={onFinish}>{t("setup.publicUrl.skip")}</Button>
        </div>
      </form>
    </section>
  );
}
