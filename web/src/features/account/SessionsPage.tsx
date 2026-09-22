import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { deleteMyData, listMySessions, revokeMySession, type Session } from "../../api/operations";
import { useAuth } from "../../auth/AuthProvider";
import { ConfirmDialog } from "../../components/Dialog";
import { Button, CheckboxField, ErrorAlert, Loading } from "../../components/ui";
import { formatDateTime } from "../../lib/format";

const SESSIONS_KEY = ["me", "sessions"] as const;

export function SessionsPage(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const { signOut } = useAuth();
  const [confirm, setConfirm] = useState<Session | null>(null);
  const [understood, setUnderstood] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const sessions = useQuery({ queryKey: SESSIONS_KEY, queryFn: listMySessions, retry: false });

  const revoke = useMutation({
    mutationFn: (session: Session) => revokeMySession(session.id),
    onSuccess: (_result, session) => {
      if (session.current) void signOut();
      else void queryClient.invalidateQueries({ queryKey: SESSIONS_KEY });
    },
  });

  const remove = useMutation({
    mutationFn: deleteMyData,
    onSuccess: () => {
      void signOut();
    },
  });

  return (
    <main id="main" className="page">
      <h1>{t("sessions.title")}</h1>
      <p>{t("sessions.help")}</p>
      <ErrorAlert error={sessions.error ?? revoke.error} />

      {sessions.isPending ? (
        <Loading />
      ) : (sessions.data ?? []).length === 0 ? (
        <p>{t("sessions.empty")}</p>
      ) : (
        <ul className="list">
          {(sessions.data ?? []).map((session) => (
            <li key={session.id} className="card">
              <p className="session-name">
                {session.device_name}
                {session.current && <span className="badge">{t("sessions.current")}</span>}
              </p>
              <p className="hint">
                {t(`sessions.kind.${session.kind}`)} · {session.platform}
              </p>
              <p className="hint">
                {t("sessions.created", { date: formatDateTime(session.created_at, i18n.language) })}
                {" · "}
                {t("sessions.lastSeen", {
                  date: formatDateTime(session.last_seen_at, i18n.language),
                })}
              </p>
              <Button
                variant="danger"
                onClick={() => {
                  setConfirm(session);
                }}
              >
                {t("common:actions.revoke")}
              </Button>
            </li>
          ))}
        </ul>
      )}

      <section className="card danger-zone">
        <h2>{t("deleteData.title")}</h2>
        <p>{t("deleteData.help")}</p>
        <ErrorAlert error={remove.error} />
        <CheckboxField
          label={t("deleteData.understand")}
          checked={understood}
          onChange={(event) => {
            setUnderstood(event.target.checked);
          }}
        />
        <Button
          variant="danger"
          disabled={!understood || remove.isPending}
          onClick={() => {
            setConfirmDelete(true);
          }}
        >
          {t("deleteData.submit")}
        </Button>
      </section>

      <ConfirmDialog
        open={confirm !== null}
        title={t("common:actions.revoke")}
        message={
          confirm?.current === true
            ? t("sessions.revokeCurrentWarning")
            : t("sessions.revokeConfirm", { device: confirm?.device_name ?? "" })
        }
        onCancel={() => {
          setConfirm(null);
        }}
        onConfirm={() => {
          if (confirm !== null) revoke.mutate(confirm);
          setConfirm(null);
        }}
      />

      <ConfirmDialog
        open={confirmDelete}
        title={t("deleteData.title")}
        message={t("deleteData.confirm")}
        confirmLabel={t("deleteData.submit")}
        onCancel={() => {
          setConfirmDelete(false);
        }}
        onConfirm={() => {
          setConfirmDelete(false);
          remove.mutate();
        }}
      />
    </main>
  );
}
