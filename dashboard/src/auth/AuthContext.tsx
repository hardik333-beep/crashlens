// A tiny auth context: it mirrors the localStorage token into React state so the
// UI re-renders on sign-in and sign-out. The API client still reads the token
// straight from localStorage (it runs outside React), and this context keeps the
// two in step.
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { clearToken, getToken, setToken } from "../lib/token";

interface AuthContextValue {
  token: string | null;
  isAuthenticated: boolean;
  signIn: (token: string) => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());

  const signIn = useCallback((next: string) => {
    setToken(next);
    setTokenState(next);
  }, []);

  const signOut = useCallback(() => {
    clearToken();
    setTokenState(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ token, isAuthenticated: token !== null, signIn, signOut }),
    [token, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used within an AuthProvider.");
  }
  return value;
}
