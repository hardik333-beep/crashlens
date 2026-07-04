import { useParams } from "react-router-dom";

import { OrgNav } from "../components/OrgNav";
import { EmptyState } from "../components/StateViews";

export function SettingsPage() {
  const { orgId = "" } = useParams();
  return (
    <section className="stack">
      <OrgNav orgId={orgId} />
      <h1>Settings</h1>
      <EmptyState title="Settings arrive in a later version.">
        <p className="muted">
          Organization preferences, error retention, and notifications will live
          here.
        </p>
      </EmptyState>
    </section>
  );
}
