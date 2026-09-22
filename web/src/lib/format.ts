/** Formats an ISO instant in the console's language; invalid input stays readable. */
export function formatDateTime(value: string | null | undefined, language: string): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** Whole seconds left before an instant, never negative. */
export function secondsUntil(value: string | null | undefined, now = Date.now()): number {
  if (value === null || value === undefined) return 0;
  const end = new Date(value).getTime();
  if (Number.isNaN(end)) return 0;
  return Math.max(0, Math.ceil((end - now) / 1000));
}
