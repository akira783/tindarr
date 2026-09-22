import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router";

import { setFetchImpl } from "../src/api/client";
import { App } from "../src/app/App";
import { AuthProvider } from "../src/auth/AuthProvider";
import { ReauthProvider } from "../src/auth/ReauthDialog";
import { initI18n } from "../src/i18n";
import type { MockApi } from "./mock-api";

const i18n = initI18n("en");

export function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

interface Options {
  api?: MockApi;
  route?: string;
  queryClient?: QueryClient;
}

/** Renders one component inside the providers the console gives every page. */
export function renderWithProviders(ui: ReactNode, options: Options = {}): RenderResult {
  if (options.api !== undefined) setFetchImpl(options.api.fetch);
  const client = options.queryClient ?? testQueryClient();
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[options.route ?? "/"]}>
          <AuthProvider>
            <ReauthProvider>{ui}</ReauthProvider>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}

/** Renders the whole console, routes and guards included. */
export function renderApp(options: Options = {}): RenderResult {
  if (options.api !== undefined) setFetchImpl(options.api.fetch);
  const client = options.queryClient ?? testQueryClient();
  return render(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter initialEntries={[options.route ?? "/"]}>
        <App queryClient={client} />
      </MemoryRouter>
    </I18nextProvider>,
  );
}

export { i18n };
