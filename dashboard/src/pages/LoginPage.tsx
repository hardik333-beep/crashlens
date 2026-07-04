import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../lib/api";
import { login } from "../lib/endpoints";

export function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await login(email, password);
      signIn(result.token);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not sign you in.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-card">
      <h1>Sign in</h1>
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
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error !== null && (
          <p role="alert" className="error-text">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary" disabled={submitting}>
          {submitting ? "Signing in..." : "Sign in"}
        </button>
      </form>
      <p className="muted">
        New here? <Link to="/signup">Create an account</Link>
      </p>
    </div>
  );
}
