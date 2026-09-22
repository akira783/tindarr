import { useCallback, useState, type SyntheticEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  getPlexPinStatus,
  saveSetupMediaServer,
  type ConnectorStatus,
  type MediaServerConfigInput,
  type MediaServerKind,
  type SetupState,
  type Waiting,
} from "../../api/operations";
import { Alert, Button, CheckboxField, ErrorAlert, SelectField, TextField } from "../../components/ui";
import { PlexPinFlow } from "../sign-in/flows";

interface OwnerToken {
  pinId: string;
  account: string | null;
}

export function MediaServerStep({
  state,
  onSaved,
}: {
  state: SetupState;
  onSaved: (status: ConnectorStatus) => void;
}): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [kind, setKind] = useState<MediaServerKind>(state.media_server?.kind ?? "jellyfin");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [verifyTls, setVerifyTls] = useState(true);
  const [owner, setOwner] = useState<OwnerToken | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [status, setStatus] = useState<ConnectorStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const locked = new Set(state.locked_fields);
  const lockedNote = t("setup.mediaServer.lockedField");

  const completeOwnerPin = useCallback(
    async (pinId: string): Promise<OwnerToken | Waiting> => {
      const result = await getPlexPinStatus(pinId);
      return result.status === "authorized"
        ? { pinId, account: result.account_name ?? null }
        : { waiting: true, retryAfterMs: 1000 };
    },
    [],
  );

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setError(null);
    setStatus(null);
    setBusy(true);

    // `verify_tls` has a default in the contract, so the generated type makes it
    // required; a field locked by the environment must not be sent with another
    // value, hence the narrower body type here.
    const body = {
      connector: "media_server",
      server_type: kind,
      url,
      ...(locked.has("verify_tls") ? {} : { verify_tls: verifyTls }),
      ...(kind === "plex"
        ? owner === null
          ? {}
          : { plex_pin_id: owner.pinId }
        : locked.has("api_key") || apiKey === ""
          ? {}
          : { api_key: apiKey }),
    };

    saveSetupMediaServer(body as MediaServerConfigInput)
      .then((result) => {
        setStatus(result);
        onSaved(result);
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
      <h2>{t("setup.mediaServer.title")}</h2>
      <p>{t("setup.mediaServer.help")}</p>
      <ErrorAlert error={error} />
      {status !== null && (
        <Alert kind={status.health === "ok" ? "success" : "warning"}>
          {t(`setup.mediaServer.health.${status.health}`)}
          {status.server_name != null ? ` — ${status.server_name}` : ""}
        </Alert>
      )}
      <form onSubmit={submit} className="flow">
        <SelectField
          label={t("setup.mediaServer.kind")}
          value={kind}
          disabled={locked.has("server_type")}
          locked={locked.has("server_type")}
          lockedNote={lockedNote}
          onChange={(event) => {
            setKind(event.target.value as MediaServerKind);
          }}
          options={[
            { value: "jellyfin", label: "Jellyfin" },
            { value: "emby", label: "Emby" },
            { value: "plex", label: "Plex" },
          ]}
        />
        <TextField
          label={t("setup.mediaServer.url")}
          hint={t("setup.mediaServer.urlHint")}
          type="url"
          required
          value={url}
          locked={locked.has("url")}
          lockedNote={lockedNote}
          onChange={(event) => {
            setUrl(event.target.value);
          }}
        />
        {kind === "plex" ? (
          <div className="flow">
            <p className="hint">{t("setup.mediaServer.plex.help")}</p>
            {owner === null ? (
              <PlexPinFlow
                purpose="owner_token"
                complete={completeOwnerPin}
                onDone={setOwner}
                labels={{
                  start: t("setup.mediaServer.plex.start"),
                  waiting: t("setup.mediaServer.plex.waiting"),
                }}
              />
            ) : (
              <Alert kind="success">
                {t("setup.mediaServer.plex.linked", {
                  account: owner.account ?? t("common:state.none"),
                })}
              </Alert>
            )}
          </div>
        ) : (
          <TextField
            label={t("setup.mediaServer.apiKey")}
            type="password"
            autoComplete="off"
            value={apiKey}
            disabled={locked.has("api_key")}
            locked={locked.has("api_key")}
            lockedNote={lockedNote}
            onChange={(event) => {
              setApiKey(event.target.value);
            }}
          />
        )}
        <CheckboxField
          label={t("setup.mediaServer.verifyTls")}
          checked={verifyTls}
          disabled={locked.has("verify_tls")}
          onChange={(event) => {
            setVerifyTls(event.target.checked);
          }}
        />
        <Button
          type="submit"
          variant="primary"
          disabled={busy || (kind === "plex" && owner === null)}
        >
          {t("setup.mediaServer.test")}
        </Button>
      </form>
    </section>
  );
}
