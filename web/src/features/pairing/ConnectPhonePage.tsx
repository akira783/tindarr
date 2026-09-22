import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QRCodeSVG } from "qrcode.react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  approvePairing,
  createPairing,
  getPairing,
  revokePairing,
  type NewPairing,
  type Pairing,
} from "../../api/operations";
import { Alert, Button, ErrorAlert } from "../../components/ui";
import { secondsUntil } from "../../lib/format";
import { isLikelyPhone, pairingHost } from "../../lib/url";

//: Used only until the first answer arrives; after that the server says how long to
//: wait, and `retry_after_ms` is null once nothing more can happen.
const FIRST_POLL_MS = 2000;

/**
 * "Connect a phone" (docs/auth.md, section 9): the QR code is drawn as SVG
 * elements, never injected as markup, and the host of `public_url` is written
 * next to it so the user sees where the phone will connect.
 */
export function ConnectPhonePage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();
  const [pairing, setPairing] = useState<NewPairing | null>(null);

  const create = useMutation({
    mutationFn: createPairing,
    onSuccess: (result) => {
      setPairing(result);
    },
  });

  const status = useQuery({
    queryKey: ["pairing", pairing?.id ?? ""],
    queryFn: () => getPairing(pairing?.id ?? ""),
    enabled: pairing !== null,
    retry: false,
    // The server decides the rhythm and says when to stop: `retry_after_ms` is null
    // once the pairing is completed, expired or revoked. Not knowing yet (no answer
    // yet) and knowing there is nothing left to wait for are different things.
    refetchInterval: (query) => {
      const answer = query.state.data;
      if (answer === undefined) return FIRST_POLL_MS;
      return answer.retry_after_ms ?? false;
    },
  });

  const approve = useMutation({
    mutationFn: (id: string) => approvePairing(id),
    onSuccess: (result) => {
      queryClient.setQueryData(["pairing", result.id], result);
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) => revokePairing(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["pairing", pairing?.id ?? ""] });
    },
  });

  const current: Pairing | null = status.data ?? pairing ?? null;
  const host = pairing === null ? null : pairingHost(pairing.link);
  const showQr = pairing !== null && host !== null && !isLikelyPhone();

  return (
    <main id="main" className="page page-narrow">
      <h1>{t("pairing.title")}</h1>
      <p>{t("pairing.help")}</p>
      <ErrorAlert error={create.error ?? approve.error ?? revoke.error ?? status.error} />

      {pairing === null ? (
        <Button
          variant="primary"
          disabled={create.isPending}
          onClick={() => {
            create.mutate();
          }}
        >
          {t("pairing.create")}
        </Button>
      ) : (
        <section className="card pairing">
          <p className="pairing-host">{t("pairing.connectsTo", { host: host ?? "?" })}</p>
          {showQr && (
            <QRCodeSVG
              value={pairing.link}
              size={232}
              level="M"
              marginSize={2}
              title={t("pairing.qrAlt", { host })}
            />
          )}
          <p>
            <a href={pairing.link}>{t("pairing.openInApp")}</a>
          </p>
          <p className="hint">
            {t("pairing.expiresIn", { seconds: secondsUntil(pairing.expires_at) })}
          </p>

          {current !== null && <PairingStatus pairing={current} />}

          {current?.status === "awaiting_approval" && (
            <div className="row">
              <Button
                variant="primary"
                onClick={() => {
                  approve.mutate(current.id);
                }}
              >
                {t("common:actions.approve")}
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  revoke.mutate(current.id);
                }}
              >
                {t("common:actions.reject")}
              </Button>
            </div>
          )}

          {current?.status === "completed" && (
            <Button
              variant="danger"
              onClick={() => {
                revoke.mutate(current.id);
              }}
            >
              {t("pairing.revokeSession")}
            </Button>
          )}

          <Button
            onClick={() => {
              setPairing(null);
              create.reset();
            }}
          >
            {t("pairing.again")}
          </Button>
        </section>
      )}
    </main>
  );
}

function PairingStatus({ pairing }: { pairing: Pairing }): ReactNode {
  const { t } = useTranslation(["console", "common"]);

  if (pairing.status === "awaiting_approval") {
    return (
      <div className="approval">
        <Alert kind="warning">{t("pairing.status.awaiting_approval")}</Alert>
        <dl>
          <dt>{t("pairing.device")}</dt>
          <dd>{pairing.device?.name ?? "—"}</dd>
          <dt>{t("pairing.platform")}</dt>
          <dd>{pairing.device?.platform ?? "—"}</dd>
          <dt>{t("pairing.from")}</dt>
          <dd>{pairing.requested_from ?? "—"}</dd>
        </dl>
        <p className="code">
          {t("pairing.confirmationCode", { code: pairing.confirmation_code ?? "????" })}
        </p>
      </div>
    );
  }

  if (pairing.status === "completed") {
    return (
      <Alert kind="success">
        {t("pairing.status.completed", { device: pairing.device?.name ?? "" })}
      </Alert>
    );
  }

  return <Alert kind="info">{t(`pairing.status.${pairing.status}`)}</Alert>;
}
