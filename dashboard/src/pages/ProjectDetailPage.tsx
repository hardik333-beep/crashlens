import { useCallback, useState } from "react";
import type { ChangeEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { CopyButton } from "../components/CopyButton";
import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import {
  createKey,
  fetchProject,
  revokeKey,
  updateProjectSampling,
} from "../lib/endpoints";
import type { DsnKey } from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";
import { useOrgRole } from "../lib/useOrg";

// The only four sampling rates the UI offers, in plain language (no
// "sampling rate" jargon in the visible option text).
const SAMPLING_OPTIONS: ReadonlyArray<{ rate: number; label: string }> = [
  { rate: 1.0, label: "Keep every event (100%)" },
  { rate: 0.5, label: "Keep half of events (50%)" },
  { rate: 0.25, label: "Keep a quarter of events (25%)" },
  { rate: 0.1, label: "Keep one in ten events (10%)" },
];

function SamplingControl({
  orgId,
  projectId,
  samplingRate,
  isAdmin,
  onUpdated,
}: {
  orgId: string;
  projectId: string;
  samplingRate: number;
  isAdmin: boolean;
  onUpdated: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const percentKept = Math.round(samplingRate * 100);

  const onChange = useCallback(
    async (event: ChangeEvent<HTMLSelectElement>) => {
      const rate = Number(event.target.value);
      setError(null);
      setBusy(true);
      try {
        await updateProjectSampling(orgId, projectId, rate);
        onUpdated();
      } catch {
        setError(
          "Could not update how many events are kept. Please try again.",
        );
      } finally {
        setBusy(false);
      }
    },
    [orgId, projectId, onUpdated],
  );

  return (
    <div className="stack">
      <h2>Keep every event, or keep a sample of events</h2>
      {isAdmin ? (
        <label className="field">
          <span>How many events to keep</span>
          <select
            value={samplingRate}
            onChange={(e) => void onChange(e)}
            disabled={busy}
          >
            {SAMPLING_OPTIONS.map((option) => (
              <option key={option.rate} value={option.rate}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      ) : (
        <p className="muted">
          An administrator can change how many events are kept.
        </p>
      )}
      <p className="muted">Keeping {percentKept}% of incoming events.</p>
      {error !== null && <ErrorView message={error} />}
    </div>
  );
}

function InstallSnippet({
  ingestUrl,
  publicKey,
}: {
  ingestUrl: string;
  publicKey: string;
}) {
  const snippet = `# Send errors to this project\n#   endpoint: ${ingestUrl}\n#   key:      ${publicKey}\n#\n# Ready-made install packages arrive in a later version.`;
  return (
    <div className="snippet-block">
      <div className="row-between">
        <p className="card-title">Connect your app</p>
        <CopyButton value={snippet} label="Copy details" />
      </div>
      <pre className="snippet">{snippet}</pre>
      <p className="muted">Install packages arrive in a later version.</p>
    </div>
  );
}

export function ProjectDetailPage() {
  const { orgId = "", projectId = "" } = useParams();
  const role = useOrgRole(orgId);
  const isAdmin = role.state.kind === "success" && role.state.data === "admin";

  const project = useAsyncData(
    () => fetchProject(orgId, projectId),
    [orgId, projectId],
  );
  const reloadProject = project.reload;

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const ingestUrl = `${window.location.origin}/api/ingest/${projectId}/`;

  const onCreateKey = useCallback(async () => {
    setActionError(null);
    setBusy(true);
    try {
      await createKey(orgId, projectId);
      reloadProject();
    } catch {
      setActionError("Could not create a key. Please try again.");
    } finally {
      setBusy(false);
    }
  }, [orgId, projectId, reloadProject]);

  const onRevokeKey = useCallback(
    async (key: DsnKey) => {
      const confirmed = window.confirm(
        "Revoke this key? Apps still using it will stop being able to send errors.",
      );
      if (!confirmed) {
        return;
      }
      setActionError(null);
      setBusy(true);
      try {
        await revokeKey(orgId, projectId, key.id);
        reloadProject();
      } catch {
        setActionError("Could not revoke the key. Please try again.");
      } finally {
        setBusy(false);
      }
    },
    [orgId, projectId, reloadProject],
  );

  if (project.state.kind === "loading") {
    return (
      <section className="stack">
        <OrgNav orgId={orgId} />
        <LoadingView />
      </section>
    );
  }
  if (project.state.kind === "error") {
    return (
      <section className="stack">
        <OrgNav orgId={orgId} />
        <ErrorView message={project.state.message} />
        <Link className="link" to={`/org/${orgId}/projects`}>
          Back to projects
        </Link>
      </section>
    );
  }

  const detail = project.state.data;
  const firstKey = detail.keys[0];

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <div className="row-between">
        <div>
          <h1>{detail.name}</h1>
          <p className="muted">{detail.platform ?? "No platform set"}</p>
        </div>
        <div className="row">
          <Link
            className="btn btn-ghost"
            to={`/org/${orgId}/projects/${projectId}/issues`}
          >
            View errors
          </Link>
          {isAdmin && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void onCreateKey()}
              disabled={busy}
            >
              {busy ? "Working..." : "Create key"}
            </button>
          )}
        </div>
      </div>

      {actionError !== null && <ErrorView message={actionError} />}

      {detail.keys.length === 0 ? (
        <EmptyState title="Create a key to start receiving errors">
          <p className="muted">
            {isAdmin
              ? "A key lets your app send its errors to this project."
              : "An administrator can create a key for this project."}
          </p>
        </EmptyState>
      ) : (
        <>
          {firstKey !== undefined && (
            <InstallSnippet
              ingestUrl={ingestUrl}
              publicKey={firstKey.public_key}
            />
          )}
          <div className="stack">
            <h2>Keys</h2>
            <ul className="card-list">
              {detail.keys.map((dsnKey) => (
                <li key={dsnKey.id} className="card">
                  <div className="key-value">
                    <code className="mono">{dsnKey.public_key}</code>
                  </div>
                  <div className="row">
                    <CopyButton value={dsnKey.public_key} label="Copy key" />
                    {isAdmin && (
                      <button
                        type="button"
                        className="btn btn-danger"
                        onClick={() => void onRevokeKey(dsnKey)}
                        disabled={busy}
                      >
                        Revoke
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </>
      )}

      <SamplingControl
        orgId={orgId}
        projectId={projectId}
        samplingRate={detail.sampling_rate}
        isAdmin={isAdmin}
        onUpdated={reloadProject}
      />
    </section>
  );
}
