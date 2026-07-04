// Instance-admin Organizations view: every organization on the instance with
// its member and project counts. Read-only, paginated.
import { useState } from "react";

import { AdminNav } from "../components/AdminNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { listAdminOrgs } from "../lib/endpoints";
import { formatDateTime } from "../lib/format";
import { useAsyncData } from "../lib/useAsyncData";

const PER_PAGE = 25;

export function AdminOrgsPage() {
  const [page, setPage] = useState(1);
  const { state } = useAsyncData(() => listAdminOrgs(page, PER_PAGE), [page]);

  return (
    <section className="stack">
      <AdminNav />
      <h1>Organizations</h1>
      <p className="muted">Every organization on this instance.</p>

      {state.kind === "loading" && <LoadingView />}
      {state.kind === "error" && <ErrorView message={state.message} />}
      {state.kind === "success" &&
        (state.data.orgs.length === 0 ? (
          <EmptyState title="No organizations yet." />
        ) : (
          <>
            <div className="table-scroll">
              <table className="issues-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th className="num">People</th>
                    <th className="num">Projects</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.orgs.map((org) => (
                    <tr key={org.id}>
                      <td>
                        <span className="issue-title">{org.name}</span>
                        <span className="muted"> &middot; {org.slug}</span>
                      </td>
                      <td className="num">{org.member_count}</td>
                      <td className="num">{org.project_count}</td>
                      <td className="muted-cell">
                        {formatDateTime(org.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager
              page={state.data.page}
              perPage={state.data.per_page}
              total={state.data.total}
              onPage={setPage}
            />
          </>
        ))}
    </section>
  );
}

function Pager({
  page,
  perPage,
  total,
  onPage,
}: {
  page: number;
  perPage: number;
  total: number;
  onPage: (next: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / perPage));
  if (totalPages <= 1) {
    return null;
  }
  const from = (page - 1) * perPage + 1;
  const to = Math.min(total, page * perPage);
  return (
    <div className="row-between pager">
      <p className="muted">
        Showing {from} to {to} of {total}
      </p>
      <div className="row">
        <button
          type="button"
          className="btn btn-ghost"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          Previous
        </button>
        <span className="muted">
          Page {page} of {totalPages}
        </span>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={page >= totalPages}
          onClick={() => onPage(page + 1)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
