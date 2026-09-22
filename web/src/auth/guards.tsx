import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";

import { Loading } from "../components/ui";
import { useAuth } from "./AuthProvider";

/**
 * The console's entry rules (docs/architecture.md, "Skeleton"): a `web` session
 * opens the console, a `setup` session the wizard, and no session goes to the
 * claim page when the server still needs setup, to sign-in otherwise.
 */
export function RequireWebSession(): ReactNode {
  const { state, setupRequired } = useAuth();
  const location = useLocation();

  if (state.status === "loading") return <Loading />;
  if (state.status === "setup") return <Navigate to="/setup" replace />;
  if (state.status === "anonymous") {
    return (
      <Navigate to={setupRequired ? "/setup" : "/sign-in"} replace state={{ from: location.pathname }} />
    );
  }
  return <Outlet />;
}

export function RequireAdmin(): ReactNode {
  const { state, isAdmin } = useAuth();
  if (state.status === "loading") return <Loading />;
  if (!isAdmin) return <Navigate to="/connect-phone" replace />;
  return <Outlet />;
}

/** `/`: admins land on the settings, everyone else on phone pairing. */
export function HomeRedirect(): ReactNode {
  const { isAdmin } = useAuth();
  return <Navigate to={isAdmin ? "/settings" : "/connect-phone"} replace />;
}
