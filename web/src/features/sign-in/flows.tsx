import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { pollUntilDone, PollAborted } from "../../api/poll";
import {
  createPlexPin,
  createQuickConnect,
  isWaiting,
  type Waiting,
} from "../../api/operations";
import { ApiError } from "../../api/problem";
import { Alert, Button, ErrorAlert } from "../../components/ui";
import { isPlexAuthUrl } from "../../lib/url";

type Purpose = "sign_in" | "reauth" | "owner_token";
type QuickConnectPurpose = "sign_in" | "reauth";

/** Cancels the running poll when the component goes away. */
function useAbortOnUnmount(): React.RefObject<AbortController | null> {
  const ref = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      ref.current?.abort();
    },
    [],
  );
  return ref;
}

interface PlexProps<T> {
  purpose: Purpose;
  complete: (pinId: string) => Promise<T | Waiting>;
  onDone: (value: T) => void;
  /** Overrides the button and the waiting message (the setup wizard names the owner account). */
  labels?: { start: string; waiting: string };
}

/**
 * Plex PIN: the server creates the PIN, the browser opens the plex.tv page (a
 * `noopener` window, and a link when the popup is blocked), and the console
 * polls with the opaque handle, never a Plex token (docs/auth.md, section 5).
 */
export function PlexPinFlow<T>({ purpose, complete, onDone, labels }: PlexProps<T>): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [pin, setPin] = useState<{ auth_url: string; expires_at: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const abortRef = useAbortOnUnmount();

  const start = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const created = await createPlexPin(purpose);
      if (!isPlexAuthUrl(created.auth_url)) {
        setBusy(false);
        setError(new ApiError(0, "unknown", null));
        setPin(null);
        return;
      }
      setPin(created);
      globalThis.open(created.auth_url, "_blank", "noopener,noreferrer");
      const controller = new AbortController();
      abortRef.current = controller;
      const value = await pollUntilDone<T>(
        async () => {
          const result = await complete(created.pin_id);
          return isWaiting(result)
            ? { done: false, retryAfterMs: result.retryAfterMs }
            : { done: true, value: result };
        },
        {
          signal: controller.signal,
          expiresAt: created.expires_at,
          onExpired: () => new ApiError(410, "pin_expired", null),
        },
      );
      onDone(value);
    } catch (caught) {
      if (!(caught instanceof PollAborted)) setError(caught);
      setPin(null);
    } finally {
      setBusy(false);
    }
  }, [abortRef, complete, onDone, purpose]);

  return (
    <div className="flow">
      <ErrorAlert error={error} />
      {pin === null ? (
        <Button variant="primary" disabled={busy} onClick={() => void start()}>
          {labels?.start ?? t("signIn.plex.start")}
        </Button>
      ) : (
        <>
          <Alert kind="info">{labels?.waiting ?? t("signIn.plex.waiting")}</Alert>
          <p className="hint">{t("signIn.plex.popupHint")}</p>
          <a href={pin.auth_url} target="_blank" rel="noopener noreferrer">
            {t("signIn.plex.openLink")}
          </a>
        </>
      )}
    </div>
  );
}

interface QuickConnectProps<T> {
  purpose: QuickConnectPurpose;
  complete: (handle: string) => Promise<T | Waiting>;
  onDone: (value: T) => void;
}

/** Quick Connect: the code is shown here and approved in a Jellyfin client. */
export function QuickConnectFlow<T>({
  purpose,
  complete,
  onDone,
}: QuickConnectProps<T>): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const abortRef = useAbortOnUnmount();

  const start = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const created = await createQuickConnect(purpose);
      setCode(created.code);
      const controller = new AbortController();
      abortRef.current = controller;
      const value = await pollUntilDone<T>(
        async () => {
          const result = await complete(created.handle);
          return isWaiting(result)
            ? { done: false, retryAfterMs: result.retryAfterMs }
            : { done: true, value: result };
        },
        {
          signal: controller.signal,
          expiresAt: created.expires_at,
          onExpired: () => new ApiError(410, "quick_connect_expired", null),
        },
      );
      onDone(value);
    } catch (caught) {
      if (!(caught instanceof PollAborted)) setError(caught);
      setCode(null);
    } finally {
      setBusy(false);
    }
  }, [abortRef, complete, onDone, purpose]);

  return (
    <div className="flow">
      <ErrorAlert error={error} />
      {code === null ? (
        <Button variant="primary" disabled={busy} onClick={() => void start()}>
          {t("signIn.quickConnect.start")}
        </Button>
      ) : (
        <>
          <p className="code-label">{t("signIn.quickConnect.code")}</p>
          <p className="code">{code}</p>
          <p className="hint">{t("signIn.quickConnect.hint")}</p>
          <Alert kind="info">{t("signIn.quickConnect.waiting")}</Alert>
        </>
      )}
    </div>
  );
}
