// Route guard: renders the nested routes only when a session token is present,
// otherwise sends the visitor to the sign-in screen.
import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

export function RequireAuth() {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}
