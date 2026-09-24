import { useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./ui";

interface DialogProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}

/**
 * Native `<dialog>`: the browser gives the focus trap, the Escape key and the
 * backdrop, so no library injects styles or markup (the CSP allows neither).
 */
export function Dialog({ open, title, onClose, children, footer }: DialogProps): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const ref = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<Element | null>(null);

  /**
   * Give the focus back where it came from.
   *
   * `<dialog>` does that itself when it is `close()`d, but this one is unmounted
   * instead — the effect below cannot reach an element React has already removed
   * — so the focus would land on `<body>` and a keyboard user would start again
   * from the top of the page. Since the deck opens one of these on every like,
   * that is a dialog away from the card each time.
   */
  useEffect(() => {
    if (!open) return undefined;
    openerRef.current = document.activeElement;
    return () => {
      const opener = openerRef.current;
      openerRef.current = null;
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, [open]);

  useEffect(() => {
    const element = ref.current;
    if (element === null) return;
    if (open) {
      if (!element.open) {
        if (typeof element.showModal === "function") element.showModal();
        else element.setAttribute("open", "");
      }
    } else if (element.open) {
      if (typeof element.close === "function") element.close();
      else element.removeAttribute("open");
    }
  }, [open]);

  if (!open) return null;

  return (
    <dialog ref={ref} className="dialog" aria-label={title} onCancel={onClose} onClose={onClose}>
      <div className="dialog-body">
        <h2>{title}</h2>
        {children}
        <div className="dialog-footer">
          {footer}
          <Button onClick={onClose}>{t("common:actions.cancel")}</Button>
        </div>
      </div>
    </dialog>
  );
}

interface ConfirmProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
}: ConfirmProps): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  return (
    <Dialog
      open={open}
      title={title}
      onClose={onCancel}
      footer={
        <Button variant="danger" onClick={onConfirm}>
          {confirmLabel ?? t("common:actions.confirm")}
        </Button>
      }
    >
      <p>{message}</p>
    </Dialog>
  );
}
