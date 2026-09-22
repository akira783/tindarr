import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState, type SyntheticEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { isWaiting, reauthenticate, type AuthMethod } from "../api/operations";
import { Dialog } from "../components/Dialog";
import { Alert, Button, ErrorAlert, TextField } from "../components/ui";
import { PlexPinFlow, QuickConnectFlow } from "../features/sign-in/flows";
import { SESSION_KEY, useAuth } from "./AuthProvider";
import { setReauthRequester } from "./reauth-registry";

interface Done {
  reauth_expires_at: string;
}

/**
 * Step-up re-authentication (docs/auth.md, section 7): the media server
 * connector and `public_url` need a sign-in less than 5 minutes old. The fetch
 * wrapper opens this dialog on `403 reauth_required` and replays the request
 * once it succeeds.
 */
export function ReauthProvider({ children }: { children: ReactNode }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const { state, serverInfo } = useAuth();
  const [resolver, setResolver] = useState<((value: boolean) => void) | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setReauthRequester(
      () =>
        new Promise<boolean>((resolve) => {
          setError(null);
          setPassword("");
          setResolver(() => resolve);
        }),
    );
    return () => {
      setReauthRequester(null);
    };
  }, []);

  const finish = useCallback(
    (ok: boolean) => {
      if (ok) void queryClient.invalidateQueries({ queryKey: SESSION_KEY });
      resolver?.(ok);
      setResolver(null);
      setPassword("");
      setBusy(false);
    },
    [queryClient, resolver],
  );

  const submitPassword = (event: SyntheticEvent): void => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    reauthenticate({ password })
      .then((result) => {
        if (!isWaiting(result)) finish(true);
      })
      .catch((caught: unknown) => {
        setError(caught);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const completePlex = useCallback((pinId: string) => reauthenticate({ pin_id: pinId }), []);
  const completeQuickConnect = useCallback(
    (handle: string) => reauthenticate({ handle }),
    [],
  );
  const onDone = useCallback<(value: Done) => void>(() => {
    finish(true);
  }, [finish]);

  const methods: AuthMethod[] = (serverInfo?.auth_methods ?? []).filter(
    (method) => method !== "pairing",
  );
  const userName = state.status === "web" ? state.user.name : "";

  return (
    <>
      {children}
      <Dialog
        open={resolver !== null}
        title={t("reauth.title")}
        onClose={() => {
          finish(false);
        }}
      >
        <p>{t("reauth.help")}</p>
        <ErrorAlert error={error} />
        {methods.length === 0 && <Alert kind="warning">{t("reauth.noMethods")}</Alert>}
        {methods.includes("password") && (
          <form onSubmit={submitPassword} className="flow">
            <TextField
              label={t("reauth.password", { name: userName })}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
            <Button type="submit" variant="primary" disabled={busy}>
              {t("reauth.submit")}
            </Button>
          </form>
        )}
        {methods.includes("plex_pin") && (
          <PlexPinFlow purpose="reauth" complete={completePlex} onDone={onDone} />
        )}
        {methods.includes("quick_connect") && (
          <QuickConnectFlow purpose="reauth" complete={completeQuickConnect} onDone={onDone} />
        )}
      </Dialog>
    </>
  );
}
