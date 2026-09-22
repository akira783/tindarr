import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { I18nextProvider } from "react-i18next";
import { BrowserRouter } from "react-router";

import { App, createQueryClient } from "./app/App";
import { initI18n } from "./i18n";
import { readTheme } from "./lib/prefs";
import "./styles/index.css";

const theme = readTheme();
if (theme !== "system") document.documentElement.setAttribute("data-theme", theme);

const i18n = initI18n();
document.documentElement.lang = i18n.language;
i18n.on("languageChanged", (language: string) => {
  document.documentElement.lang = language;
});

const container = document.getElementById("root");
if (container === null) throw new Error("missing #root");

createRoot(container).render(
  <StrictMode>
    <I18nextProvider i18n={i18n}>
      <BrowserRouter>
        <App queryClient={createQueryClient()} />
      </BrowserRouter>
    </I18nextProvider>
  </StrictMode>,
);
