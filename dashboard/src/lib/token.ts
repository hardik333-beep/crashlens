// The single named key under which the session token lives in localStorage.
// The Playwright login pattern injects the token under exactly this key, so it
// is defined once here and imported everywhere it is read or written.
export const TOKEN_STORAGE_KEY = "crashlens.session.token";

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}
