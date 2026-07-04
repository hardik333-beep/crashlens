import { Link, Navigate } from "react-router-dom";

import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { fetchMe } from "../lib/endpoints";
import { useAsyncData } from "../lib/useAsyncData";

export function OrgOverviewPage() {
  const { state } = useAsyncData(fetchMe, []);

  if (state.kind === "loading") {
    return <LoadingView label="Loading your organizations..." />;
  }
  if (state.kind === "error") {
    return <ErrorView message={state.message} />;
  }

  const { orgs } = state.data;

  if (orgs.length === 0) {
    return (
      <EmptyState title="You are not part of any organization yet.">
        <p className="muted">
          Ask a colleague for an invitation link, or create a new account to
          start your own.
        </p>
      </EmptyState>
    );
  }

  // A single-organization user has no choice to make: take them straight in.
  if (orgs.length === 1) {
    return <Navigate to={`/org/${orgs[0].id}/projects`} replace />;
  }

  return (
    <section className="stack">
      <h1>Choose an organization</h1>
      <ul className="card-list">
        {orgs.map((org) => (
          <li key={org.id} className="card">
            <div>
              <p className="card-title">{org.name}</p>
              <p className="muted">
                Your role: {org.role === "admin" ? "Administrator" : "Member"}
              </p>
            </div>
            <Link className="btn btn-primary" to={`/org/${org.id}/projects`}>
              Open
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
