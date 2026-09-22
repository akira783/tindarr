import { useEffect, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation } from "react-router";

import { useAuth } from "../../auth/AuthProvider";
import { Loading } from "../../components/ui";
import { SignInMethods } from "./SignInMethods";

export function SignInPage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const { state, serverInfo, setupRequired, adoptSession, refreshServerInfo } = useAuth();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;

  // Which methods the server offers can change between two visits to this page (a
  // restart has not probed Quick Connect yet, an administrator switched passwords
  // off), so it is read again each time rather than once per page load.
  useEffect(refreshServerInfo, [refreshServerInfo]);

  if (state.status === "loading") return <Loading />;
  if (state.status === "web") return <Navigate to={from ?? "/"} replace />;
  if (state.status === "setup" || setupRequired) return <Navigate to="/setup" replace />;

  return (
    <main id="main" className="page page-narrow">
      <h1>{t("signIn.title")}</h1>
      <p>{t("signIn.subtitle", { server: serverInfo?.name ?? t("common:app.name") })}</p>
      <SignInMethods methods={serverInfo?.auth_methods ?? []} onSignedIn={adoptSession} />
    </main>
  );
}
