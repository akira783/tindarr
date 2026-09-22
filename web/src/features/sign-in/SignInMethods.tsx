import { useCallback, useState, type SyntheticEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  signInWithPassword,
  signInWithPlexPin,
  signInWithQuickConnect,
  type AuthMethod,
  type WebSession,
} from "../../api/operations";
import { Alert, Button, ErrorAlert, TextField } from "../../components/ui";
import { PlexPinFlow, QuickConnectFlow } from "./flows";

function PasswordForm({ onSignedIn }: { onSignedIn: (session: WebSession) => void }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    signInWithPassword(username, password)
      .then(onSignedIn)
      .catch((caught: unknown) => {
        setError(caught);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <form onSubmit={submit} className="flow">
      <ErrorAlert error={error} />
      <TextField
        label={t("signIn.password.username")}
        name="username"
        autoComplete="username"
        required
        value={username}
        onChange={(event) => {
          setUsername(event.target.value);
        }}
      />
      <TextField
        label={t("signIn.password.password")}
        name="password"
        type="password"
        autoComplete="current-password"
        value={password}
        onChange={(event) => {
          setPassword(event.target.value);
        }}
      />
      <Button type="submit" variant="primary" disabled={busy}>
        {t("common:actions.signIn")}
      </Button>
    </form>
  );
}

export function SignInMethods({
  methods,
  onSignedIn,
}: {
  methods: readonly AuthMethod[];
  onSignedIn: (session: WebSession) => void;
}): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const usable = methods.filter((method) => method !== "pairing");
  const [selected, setSelected] = useState<AuthMethod | null>(null);
  const active = selected ?? usable[0] ?? null;

  const completePlex = useCallback((pinId: string) => signInWithPlexPin(pinId), []);
  const completeQuickConnect = useCallback((handle: string) => signInWithQuickConnect(handle), []);

  if (active === null) {
    return <Alert kind="warning">{t("signIn.noMethods")}</Alert>;
  }

  return (
    <div>
      {usable.length > 1 && (
        <div className="tabs" role="tablist" aria-label={t("signIn.title")}>
          {usable.map((method) => (
            <button
              key={method}
              type="button"
              role="tab"
              id={`tab-${method}`}
              aria-selected={method === active}
              aria-controls={`panel-${method}`}
              className={method === active ? "tab tab-active" : "tab"}
              onClick={() => {
                setSelected(method);
              }}
            >
              {t(`signIn.method.${method}`)}
            </button>
          ))}
        </div>
      )}
      <div role="tabpanel" id={`panel-${active}`} aria-labelledby={`tab-${active}`}>
        {active === "password" && <PasswordForm onSignedIn={onSignedIn} />}
        {active === "plex_pin" && (
          <PlexPinFlow purpose="sign_in" complete={completePlex} onDone={onSignedIn} />
        )}
        {active === "quick_connect" && (
          <QuickConnectFlow
            purpose="sign_in"
            complete={completeQuickConnect}
            onDone={onSignedIn}
          />
        )}
      </div>
    </div>
  );
}
