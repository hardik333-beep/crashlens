// Instance-admin People view: everyone with an account, and the control to
// grant or remove instance-administrator access. The last-admin guard lives on
// the server; when it (or any other rule) rejects a change, the server's own
// message is shown inline.
import { useCallback, useState } from "react";

import { AdminNav } from "../components/AdminNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import { listAdminUsers, setInstanceAdmin } from "../lib/endpoints";
import { formatDateTime } from "../lib/format";
import type { AdminUser } from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";

const PER_PAGE = 25;

export function AdminUsersPage() {
  const [page, setPage] = useState(1);
  const { state, reload } = useAsyncData(
    () => listAdminUsers(page, PER_PAGE),
    [page],
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const onToggle = useCallback(
    async (user: AdminUser) => {
      const next = !user.is_instance_admin;
      const question = next
        ? `Give ${user.email} instance-administrator access?`
        : `Remove instance-administrator access from ${user.email}?`;
      if (!window.confirm(question)) {
        return;
      }
      setActionError(null);
      setBusyId(user.id);
      try {
        await setInstanceAdmin(user.id, next);
        reload();
      } catch (err: unknown) {
        // Surface the server's own message (e.g. the last-admin guard's 400).
        setActionError(
          err instanceof ApiError ? err.message : "Could not update access.",
        );
      } finally {
        setBusyId(null);
      }
    },
    [reload],
  );

  return (
    <section className="stack">
      <AdminNav />
      <h1>People</h1>
      <p className="muted">
        Everyone with an account. Instance administrators can see this panel and
        manage the whole instance.
      </p>

      {actionError !== null && <ErrorView message={actionError} />}

      {state.kind === "loading" && <LoadingView />}
      {state.kind === "error" && <ErrorView message={state.message} />}
      {state.kind === "success" &&
        (state.data.users.length === 0 ? (
          <EmptyState title="No people yet." />
        ) : (
          <>
            <div className="table-scroll">
              <table className="issues-table">
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Joined</th>
                    <th>Last signed in</th>
                    <th>Instance admin</th>
                  </tr>
                </thead>
                <tbody>
                  {state.data.users.map((user) => (
                    <tr key={user.id}>
                      <td className="issue-title">{user.email}</td>
                      <td className="muted-cell">
                        {formatDateTime(user.created_at)}
                      </td>
                      <td className="muted-cell">
                        {user.last_login_at === null
                          ? "Never"
                          : formatDateTime(user.last_login_at)}
                      </td>
                      <td>
                        <label className="row">
                          <input
                            type="checkbox"
                            checked={user.is_instance_admin}
                            disabled={busyId === user.id}
                            onChange={() => void onToggle(user)}
                            aria-label={
                              user.is_instance_admin
                                ? `Remove instance-administrator access from ${user.email}`
                                : `Give ${user.email} instance-administrator access`
                            }
                          />
                          <span className="badge">
                            {user.is_instance_admin ? "Yes" : "No"}
                          </span>
                        </label>
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
