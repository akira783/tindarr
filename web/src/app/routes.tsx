import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, Route, Routes } from "react-router";

import { HomeRedirect, RequireAdmin, RequireWebSession } from "../auth/guards";
import { Layout } from "../components/Layout";
import { SessionsPage } from "../features/account/SessionsPage";
import { ConnectorsPage } from "../features/connectors/ConnectorsPage";
import { DeckPage } from "../features/deck/DeckPage";
import { ConnectPhonePage } from "../features/pairing/ConnectPhonePage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { SetupPage } from "../features/setup/SetupPage";
import { SignInPage } from "../features/sign-in/SignInPage";
import { UsagePage } from "../features/usage/UsagePage";
import { UsersPage } from "../features/users/UsersPage";
import { CalibrationPage } from "../features/watched/CalibrationPage";
import { ImportsPage } from "../features/watched/ImportsPage";

function NotFound(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  return (
    <main id="main" className="page page-narrow">
      <h1>{t("notFound.title")}</h1>
      <p>{t("notFound.body")}</p>
      <Link to="/">{t("notFound.home")}</Link>
    </main>
  );
}

export function AppRoutes(): ReactNode {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/sign-in" element={<SignInPage />} />
      <Route element={<RequireWebSession />}>
        <Route element={<Layout />}>
          <Route index element={<HomeRedirect />} />
          <Route path="/deck" element={<DeckPage />} />
          <Route element={<RequireAdmin />}>
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/connectors" element={<ConnectorsPage />} />
            <Route path="/users" element={<UsersPage />} />
        <Route path="/usage" element={<UsagePage />} />
          </Route>
          <Route path="/already-seen" element={<CalibrationPage />} />
          <Route path="/imports" element={<ImportsPage />} />
          <Route path="/connect-phone" element={<ConnectPhonePage />} />
          <Route path="/sessions" element={<SessionsPage />} />
        </Route>
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
