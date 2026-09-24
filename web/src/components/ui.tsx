import { useId, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/problem";

import { asTranslate, errorMessage } from "../i18n/errors";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger";
  /** React 19 passes a ref through props; a caller that has to move focus needs it. */
  ref?: React.Ref<HTMLButtonElement>;
};

export function Button({ variant = "secondary", type = "button", ...props }: ButtonProps): ReactNode {
  return <button {...props} type={type} className={`btn btn-${variant}`} />;
}

interface FieldShell {
  label: string;
  hint?: string;
  error?: string;
  locked?: boolean;
  lockedNote?: string;
  children: (props: { id: string; describedBy: string | undefined }) => ReactNode;
}

function Field({ label, hint, error, locked, lockedNote, children }: FieldShell): ReactNode {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint !== undefined || locked === true ? hintId : null, error !== undefined ? errorId : null]
    .filter((value): value is string => value !== null)
    .join(" ");

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({ id, describedBy: describedBy === "" ? undefined : describedBy })}
      {(hint !== undefined || locked === true) && (
        <p className="hint" id={hintId}>
          {hint}
          {locked === true && lockedNote !== undefined ? ` ${lockedNote}` : null}
        </p>
      )}
      {error !== undefined && (
        <p className="error" id={errorId}>
          {error}
        </p>
      )}
    </div>
  );
}

type TextFieldProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "id"> & {
  label: string;
  hint?: string;
  error?: string;
  locked?: boolean;
  lockedNote?: string;
};

export function TextField({
  label,
  hint,
  error,
  locked,
  lockedNote,
  ...input
}: TextFieldProps): ReactNode {
  return (
    <Field label={label} hint={hint} error={error} locked={locked} lockedNote={lockedNote}>
      {({ id, describedBy }) => (
        <input
          {...input}
          id={id}
          aria-describedby={describedBy}
          aria-invalid={error === undefined ? undefined : true}
        />
      )}
    </Field>
  );
}

type SelectFieldProps = Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "id"> & {
  label: string;
  hint?: string;
  error?: string;
  locked?: boolean;
  lockedNote?: string;
  options: { value: string; label: string }[];
};

export function SelectField({
  label,
  hint,
  error,
  locked,
  lockedNote,
  options,
  ...select
}: SelectFieldProps): ReactNode {
  return (
    <Field label={label} hint={hint} error={error} locked={locked} lockedNote={lockedNote}>
      {({ id, describedBy }) => (
        <select {...select} id={id} aria-describedby={describedBy}>
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

type CheckboxProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "type" | "id"> & {
  label: string;
  hint?: string;
};

export function CheckboxField({ label, hint, ...input }: CheckboxProps): ReactNode {
  const id = useId();
  const hintId = `${id}-hint`;
  return (
    <div className="field field-check">
      <input
        {...input}
        type="checkbox"
        id={id}
        aria-describedby={hint === undefined ? undefined : hintId}
      />
      <label htmlFor={id}>{label}</label>
      {hint !== undefined && (
        <p className="hint" id={hintId}>
          {hint}
        </p>
      )}
    </div>
  );
}

export function Alert({
  kind = "info",
  children,
}: {
  kind?: "info" | "success" | "warning" | "error";
  children: ReactNode;
}): ReactNode {
  return (
    <p className={`alert alert-${kind}`} role={kind === "error" ? "alert" : "status"}>
      {children}
    </p>
  );
}

/** Renders a thrown API error as a message the user can act on. */
export function ErrorAlert({ error }: { error: unknown }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  if (error === null || error === undefined) return null;
  // A validation problem carries which field it rejected; saying only "some fields are
  // invalid" leaves the reader hunting for a stray space in a pasted URL.
  const fields = error instanceof ApiError ? error.fieldErrors : [];
  return (
    <Alert kind="error">
      {errorMessage(asTranslate(t), error)}
      {fields.length > 0 && (
        <ul className="field-errors">
          {fields.map((field) => (
            <li key={field.field}>
              <code>{field.field}</code> — {field.message}
            </li>
          ))}
        </ul>
      )}
    </Alert>
  );
}

export function Loading(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  return (
    <p className="loading" role="status">
      {t("common:state.loading")}
    </p>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <section className="card">
      <h2>{title}</h2>
      {children}
    </section>
  );
}
