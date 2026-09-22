import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, use, useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { setClientHooks } from "../api/client";
import { getCsrfToken, setCsrfToken } from "../api/csrf";
import {
  getServerInfo,
  getWebSession,
  logout,
  type ServerInfo,
  type User,
  type WebSession,
} from "../api/operations";
import { requestReauth } from "./reauth-registry";

export const SESSION_KEY = ["auth", "web-session"] as const;
export const SERVER_INFO_KEY = ["server", "info"] as const;

export type AuthState =
  | { status: "loading" }
  | { status: "web"; session: WebSession; user: User }
  | { status: "setup"; session: WebSession }
  | { status: "anonymous" };

export interface AuthContextValue {
  state: AuthState;
  serverInfo: ServerInfo | null;
  setupRequired: boolean;
  isAdmin: boolean;
  setupJustCompleted: boolean;
  clearSetupJustCompleted: () => void;
  adoptSession: (session: WebSession) => void;
  /** Re-reads `GET /server/info`; the sign-in page calls it every time it is shown. */
  refreshServerInfo: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = use(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}

export function AuthProvider({ children }: { children: ReactNode }): ReactNode {
  const queryClient = useQueryClient();
  const [setupJustCompleted, setSetupJustCompleted] = useState(false);

  const session = useQuery({
    queryKey: SESSION_KEY,
    queryFn: async () => {
      const result = await getWebSession();
      setCsrfToken(result?.csrf_token ?? null);
      return result;
    },
    retry: false,
    staleTime: 30_000,
  });

  const info = useQuery({
    queryKey: SERVER_INFO_KEY,
    queryFn: getServerInfo,
    retry: false,
    staleTime: 30_000,
  });

  // The first call the console makes is a `GET`, which needs no CSRF token, so
  // installing the hooks in an effect is early enough.
  useEffect(() => {
    setClientHooks({
      getCsrfToken,
      refreshCsrfToken: async () => {
        const fresh = await getWebSession();
        queryClient.setQueryData(SESSION_KEY, fresh);
        setCsrfToken(fresh?.csrf_token ?? null);
        return fresh?.csrf_token ?? null;
      },
      onUnauthorized: () => {
        setCsrfToken(null);
        queryClient.setQueryData(SESSION_KEY, null);
        void queryClient.invalidateQueries({ queryKey: SERVER_INFO_KEY });
      },
      requestReauth,
    });
  }, [queryClient]);

  const adoptSession = useCallback(
    (next: WebSession) => {
      setCsrfToken(next.csrf_token);
      queryClient.setQueryData(SESSION_KEY, next);
      if (next.setup_completed_now === true) setSetupJustCompleted(true);
      void queryClient.invalidateQueries({ queryKey: SERVER_INFO_KEY });
    },
    [queryClient],
  );

  const refreshServerInfo = useCallback(() => {
    // `auth_methods` gains `quick_connect` only once the background probe has read
    // `GET /QuickConnect/Enabled`, 15 s after a restart: a console loaded in those
    // first seconds would otherwise offer only a password until it is reloaded.
    void queryClient.invalidateQueries({ queryKey: SERVER_INFO_KEY });
  }, [queryClient]);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      setCsrfToken(null);
      setSetupJustCompleted(false);
      queryClient.setQueryData(SESSION_KEY, null);
      queryClient.clear();
    }
  }, [queryClient]);

  const state = useMemo<AuthState>(() => {
    if (session.isPending) return { status: "loading" };
    const current = session.data ?? null;
    if (current === null) return { status: "anonymous" };
    if (current.kind === "setup") return { status: "setup", session: current };
    const user = current.user ?? null;
    if (user === null) return { status: "anonymous" };
    return { status: "web", session: current, user };
  }, [session.isPending, session.data]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      serverInfo: info.data ?? null,
      setupRequired: info.data?.setup_required ?? false,
      isAdmin: state.status === "web" && state.user.role === "admin",
      setupJustCompleted,
      clearSetupJustCompleted: () => {
        setSetupJustCompleted(false);
      },
      adoptSession,
      refreshServerInfo,
      signOut,
    }),
    [state, info.data, setupJustCompleted, adoptSession, refreshServerInfo, signOut],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
