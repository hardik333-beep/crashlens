// A dependency-free occurrence sparkline: inline SVG bars, one per day.
//
// COHERENCE (hard project gate): this component receives ONE occurrences array
// and derives BOTH the bars AND the "N events" total from it. There is no second
// data source and no separately-passed total, so the chart and the caption can
// never disagree.
import { formatDayShort } from "../lib/format";
import type { OccurrenceDay } from "../lib/types";

export function OccurrenceChart({
  occurrences,
}: {
  occurrences: OccurrenceDay[];
}) {
  const total = occurrences.reduce((sum, day) => sum + day.count, 0);
  const max = occurrences.reduce((peak, day) => Math.max(peak, day.count), 0);

  const width = 100;
  const height = 32;
  const gap = 1.5;
  const barWidth =
    occurrences.length > 0
      ? (width - gap * (occurrences.length - 1)) / occurrences.length
      : width;

  return (
    <div className="occurrence-chart">
      <div className="row-between">
        <p className="card-title">Activity</p>
        <p className="muted">
          {total} {total === 1 ? "event" : "events"} in the last{" "}
          {occurrences.length} days
        </p>
      </div>
      <svg
        className="sparkline"
        viewBox={`0 0 ${String(width)} ${String(height)}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${String(total)} events over the last ${String(occurrences.length)} days`}
      >
        {occurrences.map((day, index) => {
          // A zero-count day still shows a faint 1px baseline so the axis reads.
          const barHeight =
            max > 0 && day.count > 0
              ? Math.max(1, (day.count / max) * height)
              : 1;
          const x = index * (barWidth + gap);
          const y = height - barHeight;
          return (
            <rect
              key={day.day}
              x={x}
              y={y}
              width={barWidth}
              height={barHeight}
              className={day.count > 0 ? "bar" : "bar bar-empty"}
            >
              <title>
                {formatDayShort(day.day)}: {day.count}{" "}
                {day.count === 1 ? "event" : "events"}
              </title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}
