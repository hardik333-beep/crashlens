// The instance-admin panel tab strip. Plain-language labels only.
import { NavLink } from "react-router-dom";

export function AdminNav() {
  const tab = ({ isActive }: { isActive: boolean }): string =>
    isActive ? "tab tab-active" : "tab";
  return (
    <nav className="org-nav" aria-label="Instance sections">
      <NavLink to="/admin" end className={tab}>
        Overview
      </NavLink>
      <NavLink to="/admin/organizations" className={tab}>
        Organizations
      </NavLink>
      <NavLink to="/admin/users" className={tab}>
        People
      </NavLink>
    </nav>
  );
}
