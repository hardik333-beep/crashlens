// Instance-admin overview: whole-instance stat tiles, server-health lines, and
// the event-storage (partitions) table. Reads the single /admin/overview
// payload. Plain language throughout ("Instance", "Server health",
// "Storage days"); no vendor names.
import { AdminNav } from "../components/AdminNav";
import { ErrorView, LoadingView } from "../components/StateViews";
import { fetchAdminOverview } from "../lib/endpoints";
import type { AdminOverview } from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";

function StatTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat-tile">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function HealthLine({ ok, label }: { ok: boolean; label: string }) {
  return (
    <div className="row">
      <span className={ok ? "dot dot-ok" : "dot dot-bad"} aria-hidden="true" />
      <span>
        {label}: {ok ? "reachable" : "not reachable"}
      </span>
    </div>
  );
}

function formatCount(value: number): string {
  return value.toLocaleString();
}

function OverviewBody({ data }: { data: AdminOverview }) {
  return (
    <>
      <div className="stat-grid">
        <StatTile label="People" value={formatCount(data.users_count)} />
        <StatTile label="Organizations" value={formatCount(data.orgs_count)} />
        <StatTile label="Projects" value={formatCount(data.projects_count)} />
        <StatTile
          label="Errors tracked"
          value={formatCount(data.issues_count)}
        />
        <StatTile
          label="Events in last 24 hours"
          value={formatCount(data.events_last_24h)}
        />
        <StatTile
          label="Jobs waiting"
          value={
            data.queue_depth === null
              ? "unknown"
              : formatCount(data.queue_depth)
          }
        />
      </div>

      <div className="stack">
        <h2>Server health</h2>
        <HealthLine ok={data.db_ok} label="Database" />
        <HealthLine ok={data.redis_ok} label="Background queue" />
      </div>

      <div className="stack">
        <h2>Event storage</h2>
        <p className="muted">
          Events are stored one day at a time. Older days are removed
          automatically once they pass every project's storage window.
        </p>
        {data.partitions.length === 0 ? (
          <p className="muted">No stored days yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="issues-table">
              <thead>
                <tr>
                  <th>Day of storage</th>
                  <th className="num">Approximate events</th>
                </tr>
              </thead>
              <tbody>
                {data.partitions.map((partition) => (
                  <tr key={partition.name}>
                    <td>{partition.name}</td>
                    <td className="num">
                      {formatCount(partition.row_estimate)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

export function AdminOverviewPage() {
  const { state } = useAsyncData(fetchAdminOverview, []);

  return (
    <section className="stack">
      <AdminNav />
      <h1>Instance overview</h1>
      {state.kind === "loading" && (
        <LoadingView label="Loading instance status..." />
      )}
      {state.kind === "error" && <ErrorView message={state.message} />}
      {state.kind === "success" && <OverviewBody data={state.data} />}
    </section>
  );
}
