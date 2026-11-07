import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getToken, setToken, setUnauthorizedHandler } from "../api/client";
import * as endpoints from "../api/endpoints";
import type { TokenResponse, UserOut } from "../api/schema";

interface AuthState {
  user: UserOut | null;
  /** True until the stored token has been checked against /api/auth/me. */
  loading: boolean;
  login(username: string, password: string): Promise<void>;
  register(username: string, password: string): Promise<void>;
  logout(): void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserOut | null>(null);
  const [loading, setLoading] = useState(() => getToken() !== null);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    if (getToken()) {
      endpoints
        .me()
        .then(setUser)
        .catch(() => logout())
        .finally(() => setLoading(false));
    }
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const accept = useCallback((response: TokenResponse) => {
    setToken(response.accessToken);
    setUser(response.user);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      login: async (username, password) => accept(await endpoints.login(username, password)),
      register: async (username, password) => accept(await endpoints.register(username, password)),
      logout,
    }),
    [user, loading, accept, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
