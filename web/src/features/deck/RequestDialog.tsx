import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { requestTitle, type Card, type RequestStatus } from "../../api/operations";
import { Dialog } from "../../components/Dialog";
import { Button, ErrorAlert } from "../../components/ui";

export interface RequestDialogProps {
  card: Card | null;
  onClose: () => void;
  onFiled: (card: Card, status: RequestStatus) => void;
}

/**
 * "You liked it — shall I ask for it?"
 *
 * It is deliberately about the card the user has just left: the deck has already
 * moved on, so a slow request backend never holds up the next verdict. Every
 * answer the contract allows — queued, awaiting approval, already requested,
 * already available — has its own sentence, and so does every refusal, through
 * the shared problem-code catalogue.
 */
export function RequestDialog({ card, onClose, onFiled }: RequestDialogProps): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const queryClient = useQueryClient();

  const file = useMutation({
    mutationFn: (target: Card) =>
      requestTitle({ media_type: target.media_type, tmdb_id: target.tmdb_id }),
    onSuccess: (status, target) => {
      void queryClient.invalidateQueries({ queryKey: ["swipe", "likes"] });
      void queryClient.invalidateQueries({ queryKey: ["swipe", "stats"] });
      onFiled(target, status);
      onClose();
    },
  });

  // A new card means a new question: whatever the previous one failed with is
  // not this one's answer.
  const { reset } = file;
  useEffect(() => {
    if (card !== null) reset();
  }, [card, reset]);

  if (card === null) return null;

  return (
    <Dialog
      open
      title={t("deck.request.title")}
      onClose={onClose}
      footer={
        <Button
          variant="primary"
          disabled={file.isPending}
          onClick={() => {
            file.mutate(card);
          }}
        >
          {t("deck.request.submit")}
        </Button>
      }
    >
      <p>{t("deck.request.body", { title: card.title })}</p>
      <ErrorAlert error={file.error} />
    </Dialog>
  );
}
