// The signed-in shell: a top bar with the product name and a sign-out control,
// wrapping every authenticated page.
import { Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

export function AppLayout() {
  const { signOut } = useAuth();
  const navigate = useNavigate();

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
        <button type="button" className="btn btn-ghost" onClick={onSignOut}>
          Sign out
        </button>
      </header>
      <main className="page">
        <Outlet />
      </main>
    </div>
  );
}
