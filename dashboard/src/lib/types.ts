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
