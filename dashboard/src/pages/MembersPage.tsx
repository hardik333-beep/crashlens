import { useCallback, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";

import { CopyButton } from "../components/CopyButton";
import { OrgNav } from "../components/OrgNav";
import { EmptyState, ErrorView, LoadingView } from "../components/StateViews";
import { ApiError } from "../lib/api";
import { createInvite, listMembers } from "../lib/endpoints";
import { useAsyncData } from "../lib/useAsyncData";
import { useOrgRole } from "../lib/useOrg";

function roleLabel(role: string): string {
  return role === "admin" ? "Administrator" : "Member";
}

export function MembersPage() {
  const { orgId = "" } = useParams();
  const role = useOrgRole(orgId);
  const isAdmin = role.state.kind === "success" && role.state.data === "admin";

  const members = useAsyncData(() => listMembers(orgId), [orgId]);

  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [formError, setFormError] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);
  const [inviteLink, setInviteLink] = useState<string | null>(null);

  const onInvite = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setFormError(null);
      setInviteLink(null);
      setInviting(true);
      try {
        const result = await createInvite(orgId, email.trim(), inviteRole);
        const link = `${window.location.origin}/invite?token=${result.token}`;
        setInviteLink(link);
        setEmail("");
      } catch (err: unknown) {
        setFormError(
          err instanceof ApiError
            ? err.message
            : "Could not create the invite.",
        );
      } finally {
        setInviting(false);
      }
    },
    [orgId, email, inviteRole],
  );

  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <h1>Team members</h1>

      {isAdmin && (
        <form onSubmit={onInvite} className="inline-form">
          <label className="field">
            <span>Email to invite</span>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="teammate@example.com"
            />
          </label>
          <label className="field">
            <span>Role</span>
            <select
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value)}
            >
              <option value="member">Member</option>
              <option value="admin">Administrator</option>
            </select>
          </label>
          <button type="submit" className="btn btn-primary" disabled={inviting}>
            {inviting ? "Creating..." : "Create invite link"}
          </button>
        </form>
      )}
      {formError !== null && <ErrorView message={formError} />}

      {inviteLink !== null && (
        <div className="snippet-block">
          <div className="row-between">
            <p className="card-title">Invite link (shown once)</p>
            <CopyButton value={inviteLink} label="Copy link" />
          </div>
          <pre className="snippet">{inviteLink}</pre>
          <p className="muted">
            Share this link with the person you invited. It is shown only once,
            so copy it now. Sending invites by email arrives in a later version.
          </p>
        </div>
      )}

      {members.state.kind === "loading" && <LoadingView />}
      {members.state.kind === "error" && (
        <ErrorView message={members.state.message} />
      )}
      {members.state.kind === "success" &&
        (members.state.data.length === 0 ? (
          <EmptyState title="No team members yet." />
        ) : (
          <ul className="card-list">
            {members.state.data.map((member) => (
              <li key={member.user_id} className="card">
                <span>{member.email}</span>
                <span className="badge">{roleLabel(member.role)}</span>
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}
