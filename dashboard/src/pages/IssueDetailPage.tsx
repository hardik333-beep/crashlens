import { useCallback, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { LevelBadge, StatusBadge } from "../components/LevelBadge";
import { OccurrenceChart } from "../components/OccurrenceChart";
import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import {
  actOnIssue,
  addIssueComment,
  assignIssue,
  deleteIssue,
  fetchIssue,
  listIssueComments,
  listMembers,
  type IssueAction,
} from "../lib/endpoints";
import { formatDateTime, formatRelative } from "../lib/format";
import type {
  Breadcrumb,
  ExceptionNode,
  IssueComment,
  StackFrame,
} from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";
import { useOrgRole } from "../lib/useOrg";

export function IssueDetailPage() {
  const { orgId = "", projectId = "", issueId = "" } = useParams();
  const role = useOrgRole(orgId);
  const isAdmin = role.state.kind === "success" && role.state.data === "admin";

  const issue = useAsyncData(
    () => fetchIssue(orgId, projectId, issueId),
    [orgId, projectId, issueId],
  );
  const reload = issue.reload;

  const members = useAsyncData(() => listMembers(orgId), [orgId]);
  const comments = useAsyncData(
    () => listIssueComments(orgId, projectId, issueId),
    [orgId, projectId, issueId],
  );
  const reloadComments = comments.reload;

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [assigning, setAssigning] = useState(false);
  const [assignError, setAssignError] = useState<string | null>(null);

  const [commentBody, setCommentBody] = useState("");
  const [posting, setPosting] = useState(false);
  const [commentError, setCommentError] = useState<string | null>(null);

  const onAction = useCallback(
    async (action: IssueAction) => {
      setActionError(null);
      setBusy(true);
      try {
        await actOnIssue(orgId, projectId, issueId, action);
        reload();
      } catch {
        setActionError("Could not update this error. Please try again.");
      } finally {
        setBusy(false);
      }
    },
    [orgId, projectId, issueId, reload],
  );

  const onDelete = useCallback(async () => {
    const confirmed = window.confirm(
      "Delete this error? Its history is removed and this cannot be undone.",
    );
    if (!confirmed) {
      return;
    }
    setActionError(null);
    setBusy(true);
    try {
      await deleteIssue(orgId, projectId, issueId);
      window.location.assign(`/org/${orgId}/projects/${projectId}/issues`);
    } catch {
      setActionError("Could not delete this error. Please try again.");
      setBusy(false);
    }
  }, [orgId, projectId, issueId]);

  const onAssign = useCallback(
    async (event: ChangeEvent<HTMLSelectElement>) => {
      const value = event.target.value;
      setAssignError(null);
      setAssigning(true);
      try {
        await assignIssue(
          orgId,
          projectId,
          issueId,
          value === "" ? null : value,
        );
        reload();
      } catch (err: unknown) {
        setAssignError(
          err instanceof ApiError
            ? err.message
            : "Could not update the assignee. Please try again.",
        );
      } finally {
        setAssigning(false);
      }
    },
    [orgId, projectId, issueId, reload],
  );

  const onAddComment = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const trimmed = commentBody.trim();
      if (trimmed === "") {
        return;
      }
      setCommentError(null);
      setPosting(true);
      try {
        await addIssueComment(orgId, projectId, issueId, trimmed);
        setCommentBody("");
        reloadComments();
      } catch (err: unknown) {
        setCommentError(
          err instanceof ApiError
            ? err.message
            : "Could not add the comment. Please try again.",
        );
      } finally {
        setPosting(false);
      }
    },
    [orgId, projectId, issueId, commentBody, reloadComments],
  );

  const backLink = `/org/${orgId}/projects/${projectId}/issues`;

  if (issue.state.kind === "loading") {
    return (
      <section className="stack">
        <OrgNav orgId={orgId} />
        <LoadingView />
      </section>
    );
  }
  if (issue.state.kind === "error") {
    return (
      <section className="stack">
        <OrgNav orgId={orgId} />
        <ErrorView message={issue.state.message} />
        <Link className="link" to={backLink}>
          Back to errors
        </Link>
      </section>
    );
  }

  const detail = issue.state.data;
  const payload = detail.latest_event?.payload;
  const exception = payload?.exception;
  const breadcrumbs = payload?.breadcrumbs ?? [];
  const tags = payload?.tags ?? {};

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <Link className="link" to={backLink}>
        Back to errors
      </Link>

      <div className="detail-header">
        <div className="row detail-badges">
          <LevelBadge level={detail.level} />
          <StatusBadge status={detail.status} />
        </div>
        <h1>{detail.title}</h1>
        <p className="muted">
          {detail.event_count} {detail.event_count === 1 ? "event" : "events"}{" "}
          &middot; first seen {formatRelative(detail.first_seen)} &middot; last
          seen {formatRelative(detail.last_seen)}
        </p>
      </div>

      <div className="row detail-actions">
        {detail.status !== "resolved" && (
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void onAction("resolve")}
          >
            Mark as fixed
          </button>
        )}
        {detail.status !== "ignored" && (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => void onAction("ignore")}
          >
            Ignore
          </button>
        )}
        {detail.status !== "unresolved" && (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={() => void onAction("reopen")}
          >
            Reopen
          </button>
        )}
        {isAdmin && (
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={() => void onDelete()}
          >
            Delete
          </button>
        )}
      </div>

      {actionError !== null && <ErrorView message={actionError} />}

      <label className="field assignee-field">
        <span>Assigned to</span>
        <select
          value={detail.assigned_to ?? ""}
          onChange={(e) => void onAssign(e)}
          disabled={assigning || members.state.kind !== "success"}
        >
          <option value="">Unassigned</option>
          {members.state.kind === "success" &&
            members.state.data.map((member) => (
              <option key={member.user_id} value={member.user_id}>
                {member.email}
              </option>
            ))}
          {/* If the assignee is no longer in this org's member list, still show
              their email so the control never silently misrepresents who the
              error is assigned to. */}
          {detail.assigned_to !== null &&
            members.state.kind === "success" &&
            !members.state.data.some(
              (m) => m.user_id === detail.assigned_to,
            ) && (
              <option value={detail.assigned_to}>
                {detail.assigned_to_email ?? detail.assigned_to}
              </option>
            )}
        </select>
      </label>
      {members.state.kind === "error" && (
        <ErrorView message="Could not load team members." />
      )}
      {assignError !== null && <ErrorView message={assignError} />}

      <OccurrenceChart occurrences={detail.occurrences} />

      {detail.latest_event !== null && (
        <p className="muted env-line">
          Environment: {detail.latest_event.environment}
          {detail.latest_event.release
            ? ` · Release: ${detail.latest_event.release}`
            : ""}
        </p>
      )}

      {Object.keys(tags).length > 0 && (
        <div className="tag-row">
          {Object.entries(tags).map(([key, value]) => (
            <span key={key} className="tag-chip">
              {key}: {value}
            </span>
          ))}
        </div>
      )}

      {exception ? (
        <div className="stack">
          <h2>What went wrong</h2>
          <ExceptionView node={exception} depth={0} />
        </div>
      ) : (
        payload?.message && (
          <div className="stack">
            <h2>Message</h2>
            <pre className="snippet">{payload.message}</pre>
          </div>
        )
      )}

      {breadcrumbs.length > 0 && (
        <div className="stack">
          <h2>Leading up to it</h2>
          <Breadcrumbs crumbs={breadcrumbs} />
        </div>
      )}

      <div className="stack">
        <h2>Recent events</h2>
        {detail.recent_events.length === 0 ? (
          <EmptyState title="No recent events." />
        ) : (
          <ul className="card-list">
            {detail.recent_events.map((event) => (
              <li key={event.event_id} className="card">
                <div>
                  <p className="card-title">
                    {formatDateTime(event.received_at)}
                  </p>
                  <p className="muted">
                    {event.environment}
                    {event.release ? ` · ${event.release}` : ""}
                  </p>
                </div>
                <LevelBadge level={event.level} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="stack">
        <h2>Comments</h2>
        {comments.state.kind === "loading" && <LoadingView />}
        {comments.state.kind === "error" && (
          <ErrorView message={comments.state.message} />
        )}
        {comments.state.kind === "success" &&
          (comments.state.data.length === 0 ? (
            <EmptyState title="No comments yet." />
          ) : (
            <ul className="card-list">
              {comments.state.data.map((comment) => (
                <CommentRow key={comment.id} comment={comment} />
              ))}
            </ul>
          ))}

        <form
          onSubmit={(e) => void onAddComment(e)}
          className="stack comment-form"
        >
          <label className="field">
            <span>Add a comment</span>
            <textarea
              className="textarea"
              rows={3}
              maxLength={5000}
              value={commentBody}
              onChange={(e) => setCommentBody(e.target.value)}
              placeholder="Add notes for your team about this error..."
            />
          </label>
          <div className="row">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={posting || commentBody.trim() === ""}
            >
              {posting ? "Adding..." : "Add comment"}
            </button>
          </div>
        </form>
        {commentError !== null && <ErrorView message={commentError} />}
      </div>
    </section>
  );
}

// One exception in the chain, then its "caused by" cause recursively. The root
// cause (deepest) is the underlying failure; each nested level is labeled.
function ExceptionView({
  node,
  depth,
}: {
  node: ExceptionNode;
  depth: number;
}) {
  const frames = node.stacktrace?.frames ?? [];
  return (
    <div className="exception-block">
      {depth > 0 && <p className="caused-by">Caused by</p>}
      <p className="exception-title">
        <span className="mono">{node.type ?? "Error"}</span>
        {node.value ? `: ${node.value}` : ""}
      </p>
      {frames.length > 0 ? (
        <ol className="frame-list">
          {/* Frames are stored crash-last: render in order so the crashing
              frame sits at the bottom, nearest the message. */}
          {frames.map((frame, index) => (
            <FrameRow key={index} frame={frame} />
          ))}
        </ol>
      ) : (
        <p className="muted">No code trace was captured for this error.</p>
      )}
      {node.cause && <ExceptionView node={node.cause} depth={depth + 1} />}
    </div>
  );
}

function FrameRow({ frame }: { frame: StackFrame }) {
  // in_app defaults to true when unspecified (matches the server fingerprint
  // rule) so first-party code is highlighted and library frames are dimmed.
  const inApp = frame.in_app !== false;
  const location = [frame.filename, frame.lineno].filter(Boolean).join(":");
  return (
    <li className={inApp ? "frame frame-inapp" : "frame frame-lib"}>
      <div className="frame-head">
        <span className="mono frame-location">{location || "unknown"}</span>
        {frame.function && (
          <span className="mono frame-fn">in {frame.function}</span>
        )}
      </div>
      {frame.context_line && (
        <pre className="frame-context">{frame.context_line}</pre>
      )}
    </li>
  );
}

function Breadcrumbs({ crumbs }: { crumbs: Breadcrumb[] }) {
  // Stored newest-last: render in order so the newest breadcrumb (closest to the
  // crash) is at the bottom, right before the trace.
  return (
    <ol className="breadcrumb-list">
      {crumbs.map((crumb, index) => (
        <li key={index} className="breadcrumb">
          <div className="row breadcrumb-head">
            {crumb.category && (
              <span className="tag-chip">{crumb.category}</span>
            )}
            {crumb.type && <span className="muted">{crumb.type}</span>}
            {crumb.timestamp && (
              <span className="muted breadcrumb-time">
                {formatDateTime(crumb.timestamp)}
              </span>
            )}
          </div>
          {crumb.message && (
            <p className="breadcrumb-message">{crumb.message}</p>
          )}
        </li>
      ))}
    </ol>
  );
}

// A single comment: author email, relative age, and body. Comments arrive
// oldest-first from the API, so rendering them in order reads top-to-bottom
// like a chat thread with the newest note at the bottom.
function CommentRow({ comment }: { comment: IssueComment }) {
  return (
    <li className="card comment-card">
      <div className="stack comment-body-block">
        <div className="row-between">
          <span className="card-title">
            {comment.author_email ?? "A former team member"}
          </span>
          <span className="muted">{formatRelative(comment.created_at)}</span>
        </div>
        <p className="comment-text">{comment.body}</p>
      </div>
    </li>
  );
}
