import { useCallback, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import { createProject, deleteProject, listProjects } from "../lib/endpoints";
import type { Project } from "../lib/types";
import { useAsyncData } from "../lib/useAsyncData";
import { useOrgRole } from "../lib/useOrg";

export function ProjectsPage() {
  const { orgId = "" } = useParams();
  const role = useOrgRole(orgId);
  const isAdmin = role.state.kind === "success" && role.state.data === "admin";

  const projects = useAsyncData(() => listProjects(orgId), [orgId]);

  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const reloadProjects = projects.reload;

  const onCreate = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setFormError(null);
      setCreating(true);
      try {
        await createProject(orgId, name.trim(), platform.trim() || null);
        setName("");
        setPlatform("");
        reloadProjects();
      } catch (err: unknown) {
        setFormError(
          err instanceof ApiError
            ? err.message
            : "Could not create the project.",
        );
      } finally {
        setCreating(false);
      }
    },
    [orgId, name, platform, reloadProjects],
  );

  const onDelete = useCallback(
    async (project: Project) => {
      const confirmed = window.confirm(
        `Delete "${project.name}"? This removes its keys and cannot be undone.`,
      );
      if (!confirmed) {
        return;
      }
      try {
        await deleteProject(orgId, project.id);
        reloadProjects();
      } catch {
        // A failed delete leaves the list unchanged; a reload re-syncs state.
        reloadProjects();
      }
    },
    [orgId, reloadProjects],
  );

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <div className="row-between">
        <h1>Projects</h1>
      </div>

      {isAdmin && (
        <form onSubmit={onCreate} className="inline-form">
          <label className="field">
            <span>Project name</span>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Payments API"
            />
          </label>
          <label className="field">
            <span>Platform (optional)</span>
            <input
              type="text"
              value={platform}
              onChange={(e) => setPlatform(e.target.value)}
              placeholder="python"
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={creating}>
            {creating ? "Adding..." : "Add project"}
          </button>
        </form>
      )}
      {formError !== null && <ErrorView message={formError} />}

      {projects.state.kind === "loading" && <LoadingView />}
      {projects.state.kind === "error" && (
        <ErrorView message={projects.state.message} />
      )}
      {projects.state.kind === "success" &&
        (projects.state.data.length === 0 ? (
          <EmptyState title="No projects yet.">
            <p className="muted">
              {isAdmin
                ? "Add your first project above to start collecting errors."
                : "An administrator can add the first project."}
            </p>
          </EmptyState>
        ) : (
          <ul className="card-list">
            {projects.state.data.map((project) => (
              <li key={project.id} className="card">
                <div>
                  <Link
                    className="card-title link"
                    to={`/org/${orgId}/projects/${project.id}`}
                  >
                    {project.name}
                  </Link>
                  <p className="muted">
                    {project.platform ?? "No platform set"}
                  </p>
                </div>
                {isAdmin && (
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => void onDelete(project)}
                  >
                    Delete
                  </button>
                )}
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}
