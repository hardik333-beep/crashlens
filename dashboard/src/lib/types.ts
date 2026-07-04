// Shapes returned by the backend, mirrored for the dashboard. Field names match
// the API responses (auth.py / projects.py) exactly.

export interface User {
  id: string;
  email: string;
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  role: string;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  platform: string | null;
  created_at: string;
}

export interface DsnKey {
  id: string;
  public_key: string;
  status: string;
  created_at: string;
}

export interface ProjectDetail extends Project {
  keys: DsnKey[];
}

export interface Member {
  user_id: string;
  email: string;
  role: string;
}

// --- Alert channels ----------------------------------------------------------
export type AlertChannelType = "email" | "slack" | "webhook";

export interface AlertChannel {
  id: string;
  type: AlertChannelType;
  project_id: string | null;
  enabled: boolean;
  // A display-safe summary of the destination. The full Slack/webhook URL is
  // never returned by the API (it can embed a token), so channels are edited by
  // replacing the URL, never by reading it back.
  target: string;
  created_at: string;
}

// --- Issues (errors) ---------------------------------------------------------
export interface IssueListItem {
  id: string;
  title: string;
  level: string;
  status: string;
  first_seen: string;
  last_seen: string;
  event_count: number;
  assigned_to: string | null;
}

export interface IssueListResult {
  issues: IssueListItem[];
  total: number;
  page: number;
  per_page: number;
}

export interface OccurrenceDay {
  day: string;
  count: number;
}

export interface RecentEvent {
  event_id: string;
  received_at: string;
  environment: string;
  release: string | null;
  level: string;
}

// A stored event payload (the normalized envelope). Only the fields the detail
// view renders are typed; unknown fields pass through untouched.
export interface StackFrame {
  filename?: string;
  function?: string;
  lineno?: number;
  colno?: number;
  in_app?: boolean;
  context_line?: string;
  pre_context?: string[];
  post_context?: string[];
}

export interface ExceptionNode {
  type?: string;
  value?: string;
  stacktrace?: { frames?: StackFrame[] };
  cause?: ExceptionNode;
}

export interface Breadcrumb {
  type?: string;
  category?: string;
  message?: string;
  timestamp?: string;
  level?: string;
}

export interface EventPayload {
  message?: string;
  platform?: string;
  exception?: ExceptionNode;
  breadcrumbs?: Breadcrumb[];
  tags?: Record<string, string>;
}

export interface LatestEvent {
  event_id: string;
  received_at: string;
  environment: string;
  release: string | null;
  level: string;
  payload: EventPayload;
}

export interface IssueDetail extends IssueListItem {
  latest_event: LatestEvent | null;
  recent_events: RecentEvent[];
  occurrences: OccurrenceDay[];
  assigned_to_email: string | null;
}

export interface IssueComment {
  id: string;
  // Both author fields are null when the authoring user was deleted; the UI
  // shows "Former teammate" in that case.
  author_id: string | null;
  author_email: string | null;
  body: string;
  created_at: string;
}

export type IssueStatusFilter =
  "unresolved" | "regressed" | "resolved" | "ignored" | "all";

export type IssueSort = "last_seen" | "first_seen" | "count";

export interface AuthResult {
  token: string;
  user: User;
  orgs: Org[];
}

export interface SignupResult {
  token: string;
  user: User;
  org: Org;
}

export interface AcceptInviteResult {
  token: string;
  user: User;
  org_id: string;
  role: string;
}

export interface MeResult {
  user: User;
  orgs: Org[];
}

export interface CreateInviteResult {
  invite: { id: string; email: string; role: string; expires_at: string };
  token: string;
}
