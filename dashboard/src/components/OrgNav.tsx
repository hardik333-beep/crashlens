// The per-organization tab strip. Plain-language labels only.
import { NavLink } from "react-router-dom";

export function OrgNav({ orgId }: { orgId: string }) {
  const tab = ({ isActive }: { isActive: boolean }): string =>
    isActive ? "tab tab-active" : "tab";
  return (
    <nav className="org-nav" aria-label="Organization sections">
      <NavLink to={`/org/${orgId}/projects`} className={tab}>
        Projects
      </NavLink>
      <NavLink to={`/org/${orgId}/members`} className={tab}>
        Team members
      </NavLink>
      <NavLink to={`/org/${orgId}/settings`} className={tab}>
        Settings
      </NavLink>
    </nav>
  );
}
