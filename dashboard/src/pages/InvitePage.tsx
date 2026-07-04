import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../lib/api";
import { acceptInvite } from "../lib/endpoints";

export function InvitePage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const token = params.get("token");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (token === null) {
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const result = await acceptInvite(token, email, password);
      signIn(result.token);
      navigate(`/org/${result.org_id}/projects`, { replace: true });
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not accept this invitation.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (token === null) {
    return (
      <div className="auth-card">
        <h1>Join your team</h1>
        <p role="alert" className="error-text">
          This invitation link is missing its code. Ask your administrator to
          send you a fresh link.
        </p>
      </div>
    );
  }

  return (
    <div className="auth-card">
      <h1>Join your team</h1>
      <p className="muted">
        Set a password to accept your invitation. Use the same email address the
        invitation was sent to.
      </p>
      <form onSubmit={onSubmit} className="stack">
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Password</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <small className="muted">At least 10 characters.</small>
        </label>
        {error !== null && (
          <p role="alert" className="error-text">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "Joining..." : "Accept invitation"}
        </button>
      </form>
    </div>
  );
}
