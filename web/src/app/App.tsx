import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { isApiError } from "../api/problem";
import { AuthProvider } from "../auth/AuthProvider";
import { ReauthProvider } from "../auth/ReauthDialog";
import { AppRoutes } from "./routes";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A refused or invalid request is never worth retrying; the wrapper
        // already handled the CSRF and re-authentication cases.
        retry: (failureCount, error) =>
          !isApiError(error) || error.status >= 500 ? failureCount < 2 : false,
        refetchOnWindowFocus: false,
        staleTime: 10_000,
      },
      mutations: { retry: false },
    },
  });
}

export function App({ queryClient }: { queryClient: QueryClient }): ReactNode {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ReauthProvider>
          <AppRoutes />
        </ReauthProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
