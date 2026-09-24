import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  listUsers,
  revokeUserSessions,
  updateUser,
  type AdminUser,
} from "../../api/operations";
import { ConfirmDialog } from "../../components/Dialog";
import { Alert, Button, ErrorAlert, Loading } from "../../components/ui";
import { formatDateTime } from "../../lib/format";

const USERS_KEY = ["admin", "users"] as const;

function statusLabel(user: AdminUser): "enabled" | "disabled_admin" | "disabled_media_server" | "disabled_unlinked" | "disabled" {
  if (user.enabled) return "enabled";
  switch (user.disabled_reason) {
    case "admin":
      return "disabled_admin";
    case "media_server":
      return "disabled_media_server";
    case "unlinked":
      return "disabled_unlinked";
    default:
      return "disabled";
  }
}

export function UsersPage(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: USERS_KEY, queryFn: listUsers, retry: false });
  const [confirmRevoke, setConfirmRevoke] = useState<AdminUser | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: USERS_KEY });
  };

  const patch = useMutation({
    mutationFn: ({
      user,
      body,
    }: {
      user: AdminUser;
      body: Parameters<typeof updateUser>[1];
    }) => updateUser(user.id, body),
    onSuccess: () => {
      setMessage(t("users.updated"));
      invalidate();
    },
  });

  const revoke = useMutation({
    mutationFn: (user: AdminUser) => revokeUserSessions(user.id),
    onSuccess: () => {
      setMessage(t("users.updated"));
      invalidate();
    },
  });

  if (users.isPending) return <Loading />;
  if (users.error !== null) return <ErrorAlert error={users.error} />;

  return (
    <main id="main" className="page">
      <h1>{t("users.title")}</h1>
      <ErrorAlert error={patch.error ?? revoke.error} />
      {message !== null && <Alert kind="success">{message}</Alert>}
      {users.data.length === 0 ? (
        <p>{t("users.empty")}</p>
      ) : (
        // A wide table cannot fit a phone: it scrolls sideways inside its own box
        // rather than making the whole page scroll.
        //
        // The rule below is about tab stops nobody can use. This one is the
        // opposite: a scrollable region that a keyboard could not otherwise reach
        // is exactly what WAI asks to be made focusable and labelled, so the
        // arrow keys can scroll it. Not every browser does it on its own yet.
        // eslint-disable-next-line jsx-a11y-x/no-noninteractive-tabindex
        <div className="table-scroll" role="region" tabIndex={0} aria-label={t("users.title")}>
        <table className="table">
          <caption className="visually-hidden">{t("users.title")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("users.columns.name")}</th>
              <th scope="col">{t("users.columns.role")}</th>
              <th scope="col">{t("users.columns.status")}</th>
              <th scope="col">{t("users.columns.lastSignIn")}</th>
              <th scope="col">{t("users.columns.limit")}</th>
              <th scope="col">{t("users.columns.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {users.data.map((user) => (
              <tr key={user.id}>
                <th scope="row">
                  {user.name}
                  {user.remote_access ? null : (
                    <span className="badge">{t("users.remoteAccessOff")}</span>
                  )}
                </th>
                <td>
                  {t(`users.role.${user.role}`)}
                  {user.media_server_admin && <span className="badge">{t("users.mediaServerAdmin")}</span>}
                  {user.promoted && <span className="badge">{t("users.promoted")}</span>}
                </td>
                <td>{t(`users.status.${statusLabel(user)}`)}</td>
                <td>
                  {user.last_sign_in_at == null
                    ? t("users.never")
                    : formatDateTime(user.last_sign_in_at, i18n.language)}
                </td>
                <td>
                  <label className="visually-hidden" htmlFor={`limit-${user.id}`}>
                    {t("users.limitLabel", { name: user.name })}
                  </label>
                  <input
                    id={`limit-${user.id}`}
                    type="number"
                    min={0}
                    className="limit-input"
                    placeholder={t("users.limitDefault")}
                    defaultValue={user.daily_generation_limit ?? ""}
                    onBlur={(event) => {
                      const raw = event.target.value.trim();
                      const next = raw === "" ? null : Number(raw);
                      if (next !== (user.daily_generation_limit ?? null)) {
                        patch.mutate({ user, body: { daily_generation_limit: next } });
                      }
                    }}
                  />
                </td>
                <td className="row">
                  <Button
                    onClick={() => {
                      patch.mutate({
                        user,
                        body: { role: user.role === "admin" ? "user" : "admin" },
                      });
                    }}
                  >
                    {user.role === "admin" ? t("users.demote") : t("users.promote")}
                  </Button>
                  <Button
                    onClick={() => {
                      patch.mutate({ user, body: { enabled: !user.enabled } });
                    }}
                  >
                    {user.enabled ? t("users.disable") : t("users.enable")}
                  </Button>
                  <Button
                    onClick={() => {
                      setConfirmRevoke(user);
                    }}
                  >
                    {t("users.revokeSessions")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}

      <ConfirmDialog
        open={confirmRevoke !== null}
        title={t("users.revokeSessions")}
        message={t("users.revokeSessionsConfirm", { name: confirmRevoke?.name ?? "" })}
        onCancel={() => {
          setConfirmRevoke(null);
        }}
        onConfirm={() => {
          if (confirmRevoke !== null) revoke.mutate(confirmRevoke);
          setConfirmRevoke(null);
        }}
      />
    </main>
  );
}
