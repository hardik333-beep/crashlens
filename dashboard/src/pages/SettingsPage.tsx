import { useCallback, useMemo, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";

import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import {
  createAlertChannel,
  deleteAlertChannel,
  listAlertChannels,
  listProjects,
  updateAlertChannel,
} from "../lib/endpoints";
import type { AlertChannel, AlertChannelType } from "../lib/types";
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
    </section>
  );
}
