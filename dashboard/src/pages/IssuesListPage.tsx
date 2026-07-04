import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { LevelBadge } from "../components/LevelBadge";
import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { listIssues } from "../lib/endpoints";
import { formatRelative } from "../lib/format";
import type { IssueSort, IssueStatusFilter } from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";

const PER_PAGE = 25;

// Status tabs, in the order shown. Labels are plain language ("errors"), never
// internal terms. "Open" maps to the unresolved status.
const STATUS_TABS: { key: IssueStatusFilter; label: string }[] = [
  { key: "unresolved", label: "Open" },
  { key: "regressed", label: "Came back" },
  { key: "resolved", label: "Fixed" },
  { key: "ignored", label: "Ignored" },
  { key: "all", label: "All" },
];

const SORT_OPTIONS: { key: IssueSort; label: string }[] = [
  { key: "last_seen", label: "Most recent" },
  { key: "first_seen", label: "First seen" },
  { key: "count", label: "Most frequent" },
];

// Per-tab empty copy so an empty list explains itself in plain language.
const EMPTY_COPY: Record<IssueStatusFilter, { title: string; body: string }> = {
  unresolved: {
    title: "No open errors.",
    body: "When your app reports one, it appears here.",
  },
  regressed: {
    title: "Nothing has come back.",
    body: "Errors you marked as fixed that happen again would show up here.",
  },
  resolved: {
    title: "No fixed errors yet.",
    body: "Errors you mark as fixed are kept here for reference.",
  },
  ignored: {
    title: "No ignored errors.",
    body: "Errors you choose to ignore are moved here.",
  },
  all: {
    title: "No errors yet.",
    body: "When your app reports one, it appears here.",
  },
};

export function IssuesListPage() {
  const { orgId = "", projectId = "" } = useParams();

  const [status, setStatus] = useState<IssueStatusFilter>("unresolved");
  const [sort, setSort] = useState<IssueSort>("last_seen");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  const issues = useAsyncData(
    () =>
      listIssues(orgId, projectId, {
        status,
        sort,
        q: query || undefined,
        page,
        perPage: PER_PAGE,
      }),
    [orgId, projectId, status, sort, query, page],
  );

  const onSearchSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    setPage(1);
    setQuery(search.trim());
  };

  const changeStatus = (next: IssueStatusFilter) => {
    setStatus(next);
    setPage(1);
  };

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <div className="row-between">
        <h1>Errors</h1>
        <Link className="link" to={`/org/${orgId}/projects/${projectId}`}>
          Project settings
        </Link>
      </div>

      <div className="status-tabs" role="tablist" aria-label="Filter errors">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={status === tab.key}
            className={status === tab.key ? "chip chip-active" : "chip"}
            onClick={() => changeStatus(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="row-between issues-toolbar">
        <form onSubmit={onSearchSubmit} className="row search-form">
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search error titles"
            aria-label="Search error titles"
          />
          <button type="submit" className="btn btn-ghost">
            Search
          </button>
        </form>
        <label className="field sort-field">
          <span>Sort by</span>
          <select
            value={sort}
            onChange={(e) => {
              setSort(e.target.value as IssueSort);
              setPage(1);
            }}
          >
            {SORT_OPTIONS.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {issues.state.kind === "loading" && <LoadingView />}
      {issues.state.kind === "error" && (
        <ErrorView message={issues.state.message} />
      )}
      {issues.state.kind === "success" &&
        (issues.state.data.issues.length === 0 ? (
          <EmptyState title={EMPTY_COPY[status].title}>
            <p className="muted">{EMPTY_COPY[status].body}</p>
          </EmptyState>
        ) : (
          <>
            <div className="table-scroll">
              <table className="issues-table">
                <thead>
                  <tr>
                    <th>Error</th>
                    <th>Severity</th>
                    <th className="num">Events</th>
                    <th>First seen</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {issues.state.data.issues.map((issue) => (
                    <tr key={issue.id}>
                      <td>
                        <Link
                          className="link issue-title"
                          to={`/org/${orgId}/projects/${projectId}/issues/${issue.id}`}
                        >
                          {issue.title}
                        </Link>
                      </td>
                      <td>
                        <LevelBadge level={issue.level} />
                      </td>
                      <td className="num">{issue.event_count}</td>
                      <td className="muted-cell">
                        {formatRelative(issue.first_seen)}
                      </td>
                      <td className="muted-cell">
                        {formatRelative(issue.last_seen)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager
              page={issues.state.data.page}
              perPage={issues.state.data.per_page}
              total={issues.state.data.total}
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
        Showing {from} to {to} of {total} errors
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
