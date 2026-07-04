// One typed function per backend endpoint the dashboard uses. Keeps the pages
// free of URL strings and request shapes.
import { apiRequest } from "./api";
import type {
  AcceptInviteResult,
  AuthResult,
  CreateInviteResult,
  DsnKey,
  IssueComment,
  IssueDetail,
  IssueListResult,
  IssueSort,
  IssueStatusFilter,
  Member,
  MeResult,
  Project,
  ProjectDetail,
  SignupResult,
} from "./types";

// --- Auth (unauthenticated: handle their own errors, no auto-redirect) -------
export function login(email: string, password: string): Promise<AuthResult> {
  return apiRequest<AuthResult>("/auth/login", {
    method: "POST",
    body: { email, password },
    authenticated: false,
  });
}

export function signup(
  email: string,
  password: string,
  orgName: string,
): Promise<SignupResult> {
  return apiRequest<SignupResult>("/auth/signup", {
    method: "POST",
    body: { email, password, org_name: orgName },
    authenticated: false,
  });
}

export function acceptInvite(
  token: string,
  email: string,
  password: string,
): Promise<AcceptInviteResult> {
  return apiRequest<AcceptInviteResult>("/auth/invites/accept", {
    method: "POST",
    body: { token, email, password },
    authenticated: false,
  });
}

export function fetchMe(): Promise<MeResult> {
  return apiRequest<MeResult>("/auth/me");
}

// --- Projects ----------------------------------------------------------------
export function listProjects(orgId: string): Promise<Project[]> {
  return apiRequest<Project[]>(`/orgs/${orgId}/projects`);
}

export function createProject(
  orgId: string,
  name: string,
  platform: string | null,
): Promise<Project> {
  return apiRequest<Project>(`/orgs/${orgId}/projects`, {
    method: "POST",
    body: { name, platform },
  });
}

export function fetchProject(
  orgId: string,
  projectId: string,
): Promise<ProjectDetail> {
  return apiRequest<ProjectDetail>(`/orgs/${orgId}/projects/${projectId}`);
}

export function deleteProject(orgId: string, projectId: string): Promise<void> {
  return apiRequest<void>(`/orgs/${orgId}/projects/${projectId}`, {
    method: "DELETE",
  });
}

// --- Keys --------------------------------------------------------------------
export function createKey(orgId: string, projectId: string): Promise<DsnKey> {
  return apiRequest<DsnKey>(`/orgs/${orgId}/projects/${projectId}/keys`, {
    method: "POST",
  });
}

export function revokeKey(
  orgId: string,
  projectId: string,
  keyId: string,
): Promise<void> {
  return apiRequest<void>(
    `/orgs/${orgId}/projects/${projectId}/keys/${keyId}/revoke`,
    { method: "POST" },
  );
}

// --- Issues (errors) ---------------------------------------------------------
export interface ListIssuesParams {
  status?: IssueStatusFilter;
  q?: string;
  sort?: IssueSort;
  page?: number;
  perPage?: number;
}

export function listIssues(
  orgId: string,
  projectId: string,
  params: ListIssuesParams = {},
): Promise<IssueListResult> {
  const query = new URLSearchParams();
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.q) {
    query.set("q", params.q);
  }
  if (params.sort) {
    query.set("sort", params.sort);
  }
  if (params.page) {
    query.set("page", String(params.page));
  }
  if (params.perPage) {
    query.set("per_page", String(params.perPage));
  }
  const suffix = query.toString();
  return apiRequest<IssueListResult>(
    `/orgs/${orgId}/projects/${projectId}/issues${suffix ? `?${suffix}` : ""}`,
  );
}

export function fetchIssue(
  orgId: string,
  projectId: string,
  issueId: string,
): Promise<IssueDetail> {
  return apiRequest<IssueDetail>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}`,
  );
}

export type IssueAction = "resolve" | "ignore" | "reopen";

export function actOnIssue(
  orgId: string,
  projectId: string,
  issueId: string,
  action: IssueAction,
): Promise<IssueDetail> {
  return apiRequest<IssueDetail>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}/${action}`,
    { method: "POST" },
  );
}

export function deleteIssue(
  orgId: string,
  projectId: string,
  issueId: string,
): Promise<void> {
  return apiRequest<void>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}`,
    { method: "DELETE" },
  );
}

export function assignIssue(
  orgId: string,
  projectId: string,
  issueId: string,
  userId: string | null,
): Promise<IssueDetail> {
  return apiRequest<IssueDetail>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}/assign`,
    { method: "POST", body: { user_id: userId } },
  );
}

export function listIssueComments(
  orgId: string,
  projectId: string,
  issueId: string,
): Promise<IssueComment[]> {
  return apiRequest<IssueComment[]>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}/comments`,
  );
}

export function addIssueComment(
  orgId: string,
  projectId: string,
  issueId: string,
  body: string,
): Promise<IssueComment> {
  return apiRequest<IssueComment>(
    `/orgs/${orgId}/projects/${projectId}/issues/${issueId}/comments`,
    { method: "POST", body: { body } },
  );
}

// --- Members -----------------------------------------------------------------
export function listMembers(orgId: string): Promise<Member[]> {
  return apiRequest<Member[]>(`/orgs/${orgId}/members`);
}

export function createInvite(
  orgId: string,
  email: string,
  role: string,
): Promise<CreateInviteResult> {
  return apiRequest<CreateInviteResult>(`/orgs/${orgId}/invites`, {
    method: "POST",
    body: { email, role },
  });
}
