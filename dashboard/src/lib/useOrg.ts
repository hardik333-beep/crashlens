// Resolves the signed-in user's role in a given organization (from /auth/me).
// Pages use it to show administrator-only controls (creating and deleting
// projects, minting keys, inviting people) only to administrators. The backend
// still enforces this; the UI gating is a courtesy so members do not see
// buttons that would only return an error.
import { fetchMe } from "./endpoints";
import { useAsyncData } from "./useAsyncData";

export function useOrgRole(orgId: string) {
  return useAsyncData(async () => {
    const me = await fetchMe();
    const org = me.orgs.find((candidate) => candidate.id === orgId);
    return org ? org.role : null;
  }, [orgId]);
}
