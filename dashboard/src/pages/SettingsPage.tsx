import { useCallback, useMemo, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";

import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import {
  createAlertChannel,
  deleteAlertChannel,
  listAlertChannels,
  listAuditLog,
  listProjects,
  updateAlertChannel,
} from "../lib/endpoints";
import { formatDateTime } from "../lib/format";
import type {
  AlertChannel,
  AlertChannelType,
  AuditLogEntry,
} from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";
import { useOrgRole } from "../lib/useOrg";

const TYPE_LABELS: Record<AlertChannelType, string> = {
  email: "Email the team",
  slack: "Post to Slack",
  webhook: "Send to a webhook",
};

function needsUrl(type: AlertChannelType): boolean {
  return type === "slack" || type === "webhook";
}

function urlLabel(type: AlertChannelType): string {
  return type === "slack" ? "Slack webhook URL" : "Webhook URL";
}

// Plain-language phrase for each audit action. MUST stay in sync with
// server/app/audit.py's ACTION_LABELS -- there is no automatic link between
// the two, so update both when an action is added.
const ACTION_LABELS: Record<string, string> = {
  "project.created": "created project",
  "project.deleted": "deleted project",
  "key.created": "created a DSN key",
  "key.revoked": "revoked a DSN key",
  "member.invited": "invited a teammate",
  "invite.accepted": "accepted an invite",
  "channel.created": "created an alert",
  "channel.updated": "updated an alert",
  "channel.deleted": "removed an alert",
  "issue.resolved": "resolved an error",
  "issue.ignored": "ignored an error",
  "issue.reopened": "reopened an error",
  "issue.deleted": "deleted an error",
  "issue.assigned": "assigned an error",
  "sampling.updated": "updated sampling",
};

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

const ACTIVITY_PER_PAGE = 25;

// A short, plain-language rendering of an audit entry's small identifying
// facts (e.g. {name: "Payments API"} -> "name: Payments API"). The backend
// guarantees ``data`` never carries a secret (see server/app/audit.py), so it
// is always safe to display in full.
function describeActivityData(data: Record<string, unknown>): string {
  const parts = Object.entries(data).map(
    ([key, value]) => `${key.replace(/_/g, " ")}: ${String(value)}`,
  );
  return parts.join(", ");
}

function activityWho(entry: AuditLogEntry): string {
  return entry.actor_email ?? "Former teammate";
}

export function SettingsPage() {
  const { orgId = "" } = useParams();
  const role = useOrgRole(orgId);
  const isAdmin = role.state.kind === "success" && role.state.data === "admin";

  const channels = useAsyncData(() => listAlertChannels(orgId), [orgId]);
  const projects = useAsyncData(() => listProjects(orgId), [orgId]);

  const projectNames = useMemo(() => {
    const map = new Map<string, string>();
    if (projects.state.kind === "success") {
      for (const project of projects.state.data) {
        map.set(project.id, project.name);
      }
    }
    return map;
  }, [projects.state]);

  const [type, setType] = useState<AlertChannelType>("email");
  const [url, setUrl] = useState("");
  const [scope, setScope] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reloadChannels = channels.reload;

  const onAdd = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setFormError(null);
      setSaving(true);
      try {
        const config: Record<string, unknown> = {};
        if (type === "slack") {
          config.webhook_url = url.trim();
        } else if (type === "webhook") {
          config.url = url.trim();
        }
        await createAlertChannel(orgId, type, config, scope || null);
        setUrl("");
        setScope("");
        setType("email");
        reloadChannels();
      } catch (err: unknown) {
        setFormError(
          err instanceof ApiError ? err.message : "Could not add the alert.",
        );
      } finally {
        setSaving(false);
      }
    },
    [orgId, type, url, scope, reloadChannels],
  );

  const onToggle = useCallback(
    async (channel: AlertChannel) => {
      try {
        await updateAlertChannel(orgId, channel.id, {
          enabled: !channel.enabled,
        });
        reloadChannels();
      } catch {
        reloadChannels();
      }
    },
    [orgId, reloadChannels],
  );

  const onDelete = useCallback(
    async (channel: AlertChannel) => {
      const confirmed = window.confirm(
        `Remove this ${TYPE_LABELS[channel.type].toLowerCase()} alert?`,
      );
      if (!confirmed) {
        return;
      }
      try {
        await deleteAlertChannel(orgId, channel.id);
        reloadChannels();
      } catch {
        reloadChannels();
      }
    },
    [orgId, reloadChannels],
  );

  const scopeLabel = (channel: AlertChannel): string => {
    if (channel.project_id === null) {
      return "All projects";
    }
    return projectNames.get(channel.project_id) ?? "One project";
  };

  const [activityPage, setActivityPage] = useState(1);
  const activity = useAsyncData(async () => {
    // Members never see this section (rendered admin-only below), so skip the
    // call entirely rather than let a member's client draw a 403 from the API.
    if (!isAdmin) {
      return { entries: [], total: 0, page: 1, per_page: ACTIVITY_PER_PAGE };
    }
    return listAuditLog(orgId, {
      page: activityPage,
      perPage: ACTIVITY_PER_PAGE,
    });
  }, [orgId, activityPage, isAdmin]);

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <h1>Settings</h1>

      <div className="stack">
        <h2>Alerts</h2>
        <p className="muted">
          Choose how your team hears about new errors and errors that come back.
        </p>

        {isAdmin && (
          <form onSubmit={onAdd} className="inline-form">
            <label className="field">
              <span>How to alert</span>
              <select
                value={type}
                onChange={(e) => {
                  setType(e.target.value as AlertChannelType);
                  setUrl("");
                }}
              >
                <option value="email">Email the team</option>
                <option value="slack">Post to Slack</option>
                <option value="webhook">Send to a webhook</option>
              </select>
            </label>

            {needsUrl(type) && (
              <label className="field">
                <span>{urlLabel(type)}</span>
                <input
                  type="url"
                  required
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://..."
                />
              </label>
            )}

            <label className="field">
              <span>Which projects</span>
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="">All projects</option>
                {projects.state.kind === "success" &&
                  projects.state.data.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
              </select>
            </label>

            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? "Adding..." : "Add alert"}
            </button>
          </form>
        )}
        {formError !== null && <ErrorView message={formError} />}

        {channels.state.kind === "loading" && <LoadingView />}
        {channels.state.kind === "error" && (
          <ErrorView message={channels.state.message} />
        )}
        {channels.state.kind === "success" &&
          (channels.state.data.length === 0 ? (
            <EmptyState title="No alerts set up.">
              <p className="muted">
                Add one so your team hears about new errors first.
              </p>
            </EmptyState>
          ) : (
            <ul className="card-list">
              {channels.state.data.map((channel) => (
                <li key={channel.id} className="card">
                  <div>
                    <p className="card-title">{TYPE_LABELS[channel.type]}</p>
                    <p className="muted">
                      {scopeLabel(channel)} &middot; {channel.target}
                    </p>
                  </div>
                  <div className="row">
                    {isAdmin ? (
                      <>
                        <label className="field">
                          <span className="badge">
                            {channel.enabled ? "On" : "Off"}
                          </span>
                          <input
                            type="checkbox"
                            checked={channel.enabled}
                            onChange={() => void onToggle(channel)}
                            aria-label={
                              channel.enabled
                                ? "Turn this alert off"
                                : "Turn this alert on"
                            }
                          />
                        </label>
                        <button
                          type="button"
                          className="btn btn-danger"
                          onClick={() => void onDelete(channel)}
                        >
                          Remove
                        </button>
                      </>
                    ) : (
                      <span className="badge">
                        {channel.enabled ? "On" : "Off"}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ))}
      </div>

      {isAdmin && (
        <div className="stack">
          <h2>Activity</h2>
          <p className="muted">
            A record of sensitive changes your team makes in this organization:
            projects, keys, alerts, members, and errors.
          </p>

          {activity.state.kind === "loading" && <LoadingView />}
          {activity.state.kind === "error" && (
            <ErrorView message={activity.state.message} />
          )}
          {activity.state.kind === "success" &&
            (activity.state.data.entries.length === 0 ? (
              <EmptyState title="No activity yet." />
            ) : (
              <>
                <div className="table-scroll">
                  <table className="issues-table">
                    <thead>
                      <tr>
                        <th>When</th>
                        <th>Who</th>
                        <th>What</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activity.state.data.entries.map((entry) => {
                        const facts = describeActivityData(entry.data);
                        return (
                          <tr key={entry.id}>
                            <td className="muted-cell">
                              {formatDateTime(entry.created_at)}
                            </td>
                            <td>{activityWho(entry)}</td>
                            <td>
                              {actionLabel(entry.action)}
                              {facts && (
                                <span className="muted"> &middot; {facts}</span>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <ActivityPager
                  page={activity.state.data.page}
                  perPage={activity.state.data.per_page}
                  total={activity.state.data.total}
                  onPage={setActivityPage}
                />
              </>
            ))}
        </div>
      )}
    </section>
  );
}

function ActivityPager({
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
