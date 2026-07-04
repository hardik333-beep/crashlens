import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./components/AppLayout";
import { RequireAuth } from "./components/RequireAuth";
import { AdminOrgsPage } from "./pages/AdminOrgsPage";
import { AdminOverviewPage } from "./pages/AdminOverviewPage";
import { AdminUsersPage } from "./pages/AdminUsersPage";
import { InvitePage } from "./pages/InvitePage";
import { IssueDetailPage } from "./pages/IssueDetailPage";
import { IssuesListPage } from "./pages/IssuesListPage";
import { LoginPage } from "./pages/LoginPage";
import { MembersPage } from "./pages/MembersPage";
import { OrgOverviewPage } from "./pages/OrgOverviewPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SignupPage } from "./pages/SignupPage";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/invite" element={<InvitePage />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          <Route path="/" element={<OrgOverviewPage />} />
          <Route path="/org/:orgId/projects" element={<ProjectsPage />} />
          <Route
            path="/org/:orgId/projects/:projectId"
            element={<ProjectDetailPage />}
          />
          <Route
            path="/org/:orgId/projects/:projectId/issues"
            element={<IssuesListPage />}
          />
          <Route
            path="/org/:orgId/projects/:projectId/issues/:issueId"
            element={<IssueDetailPage />}
          />
          <Route path="/org/:orgId/members" element={<MembersPage />} />
          <Route path="/org/:orgId/settings" element={<SettingsPage />} />
          <Route path="/admin" element={<AdminOverviewPage />} />
          <Route path="/admin/organizations" element={<AdminOrgsPage />} />
          <Route path="/admin/users" element={<AdminUsersPage />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
