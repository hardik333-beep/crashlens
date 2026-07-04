import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../lib/api";
import { signup } from "../lib/endpoints";

export function SignupPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await signup(email, password, orgName);
      signIn(result.token);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not create your account.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-card">
      <h1>Create your account</h1>
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
        <label className="field">
          <span>Organization name</span>
          <input
            type="text"
            required
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
          />
        </label>
        {error !== null && (
          <p role="alert" className="error-text">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "Creating..." : "Create account"}
        </button>
      </form>
      <p className="muted">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </div>
  );
}
