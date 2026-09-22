import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type SyntheticEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useNavigate } from "react-router";

import { claimSetup, getSetupState, type WebSession } from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { Alert, Button, ErrorAlert, Loading, TextField } from "../../components/ui";
import { SignInMethods } from "../sign-in/SignInMethods";
import { MediaServerStep } from "./MediaServerStep";
import { PublicUrlStep } from "./PublicUrlStep";

const SETUP_STATE_KEY = ["setup", "state"] as const;
const STEPS = ["claim", "mediaServer", "signIn", "publicUrl"] as const;

function StepHeader({ step }: { step: (typeof STEPS)[number] }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const index = STEPS.indexOf(step) + 1;
  return (
    <header className="wizard-header">
      <h1>{t("setup.title")}</h1>
      <p className="hint">
        {t("setup.step", { current: index, total: STEPS.length })} — {t(`setup.steps.${step}`)}
      </p>
    </header>
  );
}

function ClaimStep({ onClaimed }: { onClaimed: (session: WebSession) => void }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    claimSetup(code.trim())
      .then(onClaimed)
      .catch((caught: unknown) => {
        setError(caught);
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className="card">
      <h2>{t("setup.claim.title")}</h2>
      <p>{t("setup.claim.help")}</p>
      <ErrorAlert error={error} />
      <form onSubmit={submit} className="flow">
        <TextField
          label={t("setup.claim.label")}
          value={code}
          required
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => {
            setCode(event.target.value);
          }}
        />
        <Button type="submit" variant="primary" disabled={busy}>
          {t("setup.claim.submit")}
        </Button>
      </form>
    </section>
  );
}

function SetupSessionSteps(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const { adoptSession } = useAuth();
  const [editing, setEditing] = useState(false);
  const setupState = useQuery({ queryKey: SETUP_STATE_KEY, queryFn: getSetupState, retry: false });

  if (setupState.isPending) return <Loading />;
  if (setupState.error !== null) return <ErrorAlert error={setupState.error} />;

  const state = setupState.data;
  const configured = state.media_server !== null || state.media_server_locked;

  if (!configured || editing) {
    return (
      <>
        <StepHeader step="mediaServer" />
        <MediaServerStep
          state={state}
          onSaved={() => {
            setEditing(false);
            void queryClient.invalidateQueries({ queryKey: SETUP_STATE_KEY });
          }}
        />
      </>
    );
  }

  return (
    <>
      <StepHeader step="signIn" />
      <section className="card">
        {state.media_server_locked ? (
          <Alert kind="info">{t("setup.mediaServer.lockedAll")}</Alert>
        ) : (
          <p>
            {t("setup.mediaServer.configured", {
              name: state.media_server?.name ?? state.media_server?.kind ?? "",
            })}{" "}
            <Button
              onClick={() => {
                setEditing(true);
              }}
            >
              {t("common:actions.back")}
            </Button>
          </p>
        )}
        <h2>{t("setup.signIn.title")}</h2>
        <p>{t("setup.signIn.help")}</p>
        <SignInMethods methods={state.auth_methods} onSignedIn={adoptSession} />
      </section>
    </>
  );
}

export function SetupPage(): ReactNode {
  const navigate = useNavigate();
  const { state, setupRequired, adoptSession, setupJustCompleted, clearSetupJustCompleted } =
    useAuth();

  if (state.status === "loading") return <Loading />;

  if (state.status === "web") {
    if (!setupJustCompleted) return <Navigate to="/" replace />;
    return (
      <main id="main" className="page page-narrow">
        <StepHeader step="publicUrl" />
        <PublicUrlStep
          onFinish={() => {
            clearSetupJustCompleted();
            void navigate("/", { replace: true });
          }}
        />
      </main>
    );
  }

  if (state.status === "anonymous") {
    if (!setupRequired) return <Navigate to="/sign-in" replace />;
    return (
      <main id="main" className="page page-narrow">
        <StepHeader step="claim" />
        <ClaimStep onClaimed={adoptSession} />
      </main>
    );
  }

  return (
    <main id="main" className="page page-narrow">
      <SetupSessionSteps />
    </main>
  );
}
