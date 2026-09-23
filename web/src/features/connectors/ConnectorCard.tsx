import { useState, type ReactNode, type SyntheticEvent } from "react";
import { useTranslation } from "react-i18next";

import {
  deleteConnector,
  saveConnector,
  testConnector,
  type Connector,
  type ConnectorInput,
  type ConnectorKind,
  type ConnectorStatus,
} from "../../api/operations";
import { Alert, Button, ErrorAlert } from "../../components/ui";

/**
 * The shell every connector card shares: its health, its secret, and the three
 * things an administrator can do with it.
 *
 * "Test" and "Save" send the same body. The difference is what the server does with
 * it: a test stores nothing, which is what makes it safe to press while still typing.
 * An omitted key is only reused for the same address, so a card that moved its URL
 * gets `secret_required` back and says so.
 */
export function ConnectorCard({
  connector,
  title,
  help,
  body,
  valid = true,
  removable = true,
  children,
  onChanged,
}: {
  connector: Connector;
  title: string;
  help?: string;
  /** Builds the request body from the card's current fields. */
  body: () => ConnectorInput;
  valid?: boolean;
  removable?: boolean;
  children: ReactNode;
  onChanged: () => void;
}): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [status, setStatus] = useState<ConnectorStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const kind: ConnectorKind = connector.kind;

  const run = (action: () => Promise<void>): void => {
    setError(null);
    setStatus(null);
    setSaved(false);
    setBusy(true);
    action()
      .catch((caught: unknown) => {
        setError(caught);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    run(async () => {
      const result = await saveConnector(kind, body());
      setStatus(result.status);
      setSaved(true);
      onChanged();
    });
  };

  const check = (): void => {
    run(async () => {
      setStatus(await testConnector(kind, body()));
    });
  };

  const remove = (): void => {
    run(async () => {
      await deleteConnector(kind);
      onChanged();
    });
  };

  const shown = status ?? connector.status;

  return (
    <section className="card">
      <h2>{title}</h2>
      {help !== undefined && <p className="hint">{help}</p>}
      <ConnectorHealthAlert status={shown} />
      <ErrorAlert error={error} />
      {saved && <Alert kind="success">{t("connectors.saved")}</Alert>}
      <form onSubmit={submit} className="flow">
        {children}
        <SecretHint connector={connector} />
        <div className="row">
          <Button type="submit" variant="primary" disabled={busy || !valid}>
            {t("connectors.save")}
          </Button>
          <Button onClick={check} disabled={busy || !valid}>
            {t("connectors.test")}
          </Button>
          {removable && connector.configured && (
            <Button variant="danger" onClick={remove} disabled={busy}>
              {t("connectors.remove")}
            </Button>
          )}
        </div>
      </form>
    </section>
  );
}

export function ConnectorHealthAlert({ status }: { status: ConnectorStatus }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const kind =
    status.health === "ok"
      ? "success"
      : status.health === "not_configured" || status.health === "unknown"
        ? "info"
        : "warning";
  return (
    <Alert kind={kind}>
      {t(`connectors.health.${status.health}`)}
      {status.server_name != null ? ` — ${status.server_name}` : ""}
      {status.server_version != null ? ` ${status.server_version}` : ""}
    </Alert>
  );
}

/** What the server says about the stored secret; it never returns the secret itself. */
export function SecretHint({ connector }: { connector: Connector }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const secret = connector.secret;
  if (secret?.set !== true) {
    return <p className="hint">{t("connectors.secret.unset")}</p>;
  }
  return (
    <p className="hint">
      {secret.last4 == null
        ? t("connectors.secret.set")
        : t("connectors.secret.setEnding", { last4: secret.last4 })}
      {secret.locked === true ? ` ${t("connectors.locked")}` : ""}
    </p>
  );
}
