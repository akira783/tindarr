import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { getUsage, listUsers, type UsageDay } from "../../api/operations";
import { ErrorAlert, Loading, SelectField } from "../../components/ui";

const RANGES = [7, 30, 90] as const;
type Range = (typeof RANGES)[number];

interface Totals {
  generations: number;
  inputTokens: number;
  outputTokens: number;
  failures: number;
}

/**
 * Sums a set of rows. The server sends rows and no totals on purpose, so this is the
 * one place the console decides what to add up — and an administrator who exports the
 * table gets exactly the same numbers back.
 */
function sum(rows: readonly UsageDay[]): Totals {
  return rows.reduce<Totals>(
    (total, row) => ({
      generations: total.generations + row.generations,
      inputTokens: total.inputTokens + row.input_tokens,
      outputTokens: total.outputTokens + row.output_tokens,
      failures: total.failures + (row.failures ?? 0),
    }),
    { generations: 0, inputTokens: 0, outputTokens: 0, failures: 0 },
  );
}

/** Formats a date-only string; an unparseable one stays readable rather than "Invalid". */
function formatDay(value: string, language: string): string {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeZone: "UTC" }).format(date);
}

export function UsagePage(): ReactNode {
  const { t, i18n } = useTranslation(["console", "common"]);
  const [range, setRange] = useState<Range>(30);
  const usage = useQuery({
    queryKey: ["admin", "usage", range] as const,
    queryFn: () => getUsage(range),
    retry: false,
  });
  // The rows carry a user id, which says nothing to a person reading a bill. The user
  // list is the only place the names live, and a missing one falls back to the id
  // rather than to a blank cell: an account somebody deleted still spent money.
  const users = useQuery({ queryKey: ["admin", "users"] as const, queryFn: listUsers, retry: false });
  const names = useMemo(
    () => new Map((users.data ?? []).map((user) => [user.id, user.name])),
    [users.data],
  );

  if (usage.isPending) return <Loading />;
  if (usage.error !== null) return <ErrorAlert error={usage.error} />;

  const rows = usage.data;
  const totals = sum(rows);
  const number = new Intl.NumberFormat(i18n.language);

  return (
    <main id="main" className="page">
      <h1>{t("usage.title")}</h1>
      <p className="hint">{t("usage.intro")}</p>
      <SelectField
        label={t("usage.range")}
        value={String(range)}
        onChange={(event) => {
          setRange(Number(event.target.value) as Range);
        }}
        options={RANGES.map((days) => ({
          value: String(days),
          label: t("usage.rangeDays", { count: days }),
        }))}
      />
      <dl className="stat-row">
        <div className="stat">
          <dt>{t("usage.columns.generations")}</dt>
          <dd>{number.format(totals.generations)}</dd>
        </div>
        <div className="stat">
          <dt>{t("usage.columns.inputTokens")}</dt>
          <dd>{number.format(totals.inputTokens)}</dd>
        </div>
        <div className="stat">
          <dt>{t("usage.columns.outputTokens")}</dt>
          <dd>{number.format(totals.outputTokens)}</dd>
        </div>
        <div className="stat">
          <dt>{t("usage.columns.failures")}</dt>
          <dd>{number.format(totals.failures)}</dd>
        </div>
      </dl>
      {rows.length === 0 ? (
        <p>{t("usage.empty")}</p>
      ) : (
        <table className="table">
          <caption className="visually-hidden">{t("usage.title")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("usage.columns.date")}</th>
              <th scope="col">{t("usage.columns.user")}</th>
              <th scope="col">{t("usage.columns.generations")}</th>
              <th scope="col">{t("usage.columns.inputTokens")}</th>
              <th scope="col">{t("usage.columns.outputTokens")}</th>
              <th scope="col">{t("usage.columns.failures")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.date}-${row.user_id}`}>
                <th scope="row">{formatDay(row.date, i18n.language)}</th>
                <td>{names.get(row.user_id) ?? row.user_id}</td>
                <td>{number.format(row.generations)}</td>
                <td>{number.format(row.input_tokens)}</td>
                <td>{number.format(row.output_tokens)}</td>
                <td>{number.format(row.failures ?? 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
