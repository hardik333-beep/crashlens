// Small presentation helpers shared by the issue list and detail pages. Kept
// dependency-free (no date library) to match the project's minimal footprint.

/** Format an ISO timestamp as a short, local, human date-time. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Format an ISO timestamp as a compact relative age, e.g. "3h ago". */
export function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return iso;
  }
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${String(minutes)}m ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${String(hours)}h ago`;
  }
  const days = Math.floor(hours / 24);
  if (days < 30) {
    return `${String(days)}d ago`;
  }
  const months = Math.floor(days / 30);
  if (months < 12) {
    return `${String(months)}mo ago`;
  }
  return `${String(Math.floor(months / 12))}y ago`;
}

/** Format a YYYY-MM-DD day as a short "Jul 4" label for chart tooltips. */
export function formatDayShort(day: string): string {
  const date = new Date(`${day}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return day;
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
