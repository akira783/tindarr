import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/AuthProvider";
import { SUPPORTED_LANGUAGES, type Language } from "../i18n";
import { readTheme, writeLanguage, writeTheme, type ThemeChoice } from "../lib/prefs";
import { Button } from "./ui";

function ThemeSelect(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [theme, setTheme] = useState<ThemeChoice>(() => readTheme());

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
  }, [theme]);

  return (
    <label className="inline-field">
      <span>{t("common:theme.label")}</span>
      <select
        value={theme}
        onChange={(event) => {
          const value = event.target.value as ThemeChoice;
          setTheme(value);
          writeTheme(value);
        }}
      >
        <option value="system">{t("common:theme.system")}</option>
        <option value="light">{t("common:theme.light")}</option>
        <option value="dark">{t("common:theme.dark")}</option>
      </select>
    </label>
  );
}

function LanguageSelect(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  return (
    <label className="inline-field">
      <span>{t("common:language.label")}</span>
      <select
        value={i18n.language}
        onChange={(event) => {
          const value = event.target.value as Language;
          void i18n.changeLanguage(value);
          writeLanguage(value);
        }}
      >
        {SUPPORTED_LANGUAGES.map((language) => (
          <option key={language} value={language}>
            {t(`common:language.${language}`)}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Layout(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const { state, serverInfo, isAdmin, signOut } = useAuth();
  const userName = state.status === "web" ? state.user.name : null;

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        {t("nav.skipToContent")}
      </a>
      <header className="topbar">
        <p className="brand">{serverInfo?.name ?? t("common:app.console")}</p>
        <nav aria-label={t("common:app.console")}>
          <ul>
            {isAdmin && (
              <>
                <li>
                  <NavLink to="/settings">{t("nav.settings")}</NavLink>
                </li>
                <li>
                  <NavLink to="/users">{t("nav.users")}</NavLink>
                </li>
              </>
            )}
            <li>
              <NavLink to="/connect-phone">{t("nav.connectPhone")}</NavLink>
            </li>
            <li>
              <NavLink to="/sessions">{t("nav.sessions")}</NavLink>
            </li>
          </ul>
        </nav>
        <div className="topbar-end">
          <LanguageSelect />
          <ThemeSelect />
          {userName !== null && (
            <>
              <span className="hint">{t("nav.signedInAs", { name: userName })}</span>
              <Button
                onClick={() => {
                  void signOut();
                }}
              >
                {t("common:actions.signOut")}
              </Button>
            </>
          )}
        </div>
      </header>
      <Outlet />
    </div>
  );
}
