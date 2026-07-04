// A small colored badge for an error's severity level. Plain-language labels;
// the color is a subtle tint, not one of the banned template palettes.
const LEVEL_LABELS: Record<string, string> = {
  fatal: "Fatal",
  error: "Error",
  warning: "Warning",
  info: "Info",
  debug: "Debug",
};

export function LevelBadge({ level }: { level: string }) {
  const label = LEVEL_LABELS[level] ?? level;
  return <span className={`level-badge level-${level}`}>{label}</span>;
}

// Plain-language labels for an error's workflow status.
const STATUS_LABELS: Record<string, string> = {
  unresolved: "Open",
  regressed: "Came back",
  resolved: "Fixed",
  ignored: "Ignored",
};

export function StatusBadge({ status }: { status: string }) {
  const label = STATUS_LABELS[status] ?? status;
  return <span className={`status-badge status-${status}`}>{label}</span>;
}
