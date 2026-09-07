"use client";

/**
 * Who is logged in, for the whole app.
 *
 * The token lives in localStorage. That is a deliberate development-grade
 * choice, not an oversight: it is readable by any script on the page, so an XSS
 * bug becomes a stolen token. The alternative -- an httpOnly cookie -- needs the
 * backend to set and clear it, plus CSRF protection, and the API currently
 * issues bearer tokens for a JS client to hold. Revisit together with the
 * backend, not here alone.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, ApiError, type User } from "./api";

const TOKEN_KEY = "frc.token";

interface AuthState {
  token: string | null;
  user: User | null;
  /** False until the stored token has been checked, so the UI can avoid
   *  flashing the login page at an already-authenticated user. */
  ready: boolean;
  signup: (email: string, password: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  // On boot, adopt a stored token only if it still works. A token that expired
  // while the tab was closed would otherwise put the app in a logged-in state
  // where every request 401s.
  useEffect(() => {
    let alive = true;
    const stored =
      typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY);

    // `ready` is settled in a callback rather than in the effect body, even
    // for the no-token case: a synchronous setState here is a second render
    // before the first has painted, which is what React 19 flags.
    const check = stored
      ? api
          .me(stored)
          .then((me) => {
            if (!alive) return;
            setToken(stored);
            setUser(me);
          })
          .catch(() => localStorage.removeItem(TOKEN_KEY))
      : Promise.resolve();

    check.finally(() => {
      if (alive) setReady(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  const adopt = useCallback(async (accessToken: string) => {
    localStorage.setItem(TOKEN_KEY, accessToken);
    setToken(accessToken);
    setUser(await api.me(accessToken));
  }, []);

  const signup = useCallback(
    async (email: string, password: string) => {
      const { access_token } = await api.signup(email, password);
      await adopt(access_token);
    },
    [adopt],
  );

  const login = useCallback(
    async (email: string, password: string) => {
      const { access_token } = await api.login(email, password);
      await adopt(access_token);
    },
    [adopt],
  );

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ token, user, ready, signup, login, logout }),
    [token, user, ready, signup, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside an AuthProvider");
  return context;
}

/** Turns any thrown value into something worth showing a user. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
