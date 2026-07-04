// The signed-in shell: a top bar with the product name and a sign-out control,
// wrapping every authenticated page. Instance administrators additionally see an
// "Instance" link to the operator panel; the link is hidden for everyone else
// (the API still enforces access server-side).
import { Link, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { fetchMe } from "../lib/endpoints";
import { useAsyncData } from "../lib/useAsyncData";

export function AppLayout() {
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const { state } = useAsyncData(fetchMe, []);
  const isInstanceAdmin =
    state.kind === "success" && state.data.is_instance_admin;

  const onSignOut = () => {
    signOut();
    navigate("/login", { replace: true });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/">
          Crashlens
        </a>
        <div className="row">
          {isInstanceAdmin && (
            <Link className="btn btn-ghost" to="/admin">
              Instance
            </Link>
          )}
          <button type="button" className="btn btn-ghost" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </div>
  );
}
