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
import { useRouter } from "next/navigation";
import { api, ApiError, type User } from "./api";
import { onSessionExpired } from "./session";
import { ATTEMPT_TIMEOUT_MS, checkStoredSession } from "./sessionCheck.mjs";

const TOKEN_KEY = "frc.token";

interface AuthState {
  token: string | null;
  user: User | null;
  /** False until the stored token has been checked, so the UI can avoid
   *  flashing the login page at an already-authenticated user. */
  ready: boolean;
  /** Set when a request came back 401 rather than the user signing out, so the
   *  login page can say why they are looking at it. */
  expired: boolean;
  /** True while the stored token's check is being retried because the API is
   *  asleep or busy (no answer, 429 or 5xx); the layout shows a notice. */
  waking: boolean;
  signup: (email: string, password: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  /** Sign in as a fresh demo account. */
  demo: () => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [expired, setExpired] = useState(false);
  const [waking, setWaking] = useState(false);
  const router = useRouter();

  // Any 401 from anywhere in the app lands here. Dropping the dead token
  // matters as much as the redirect: without it a reload would try the same
  // expired token again and bounce straight back.
  useEffect(
    () =>
      onSessionExpired(() => {
        localStorage.removeItem(TOKEN_KEY);
        setToken(null);
        setUser(null);
        setExpired(true);
        router.replace("/login");
      }),
    [router],
  );

  // On boot, check the stored token. Only a 401 means it is bad: a token that
  // expired while the tab was closed would otherwise put the app in a
  // logged-in state where every request 401s. A sleeping or busy API (no
  // answer, 429, 5xx) says nothing about the token, so the session is kept and
  // the check retried with backoff (src/lib/sessionCheck.mjs). If the API never
  // answers, the token is kept unverified; any later 401 still ends the session.
  useEffect(() => {
    let alive = true;
    const stored =
      typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY);

    // `ready` is settled in a callback rather than in the effect body, even
    // for the no-token case: a synchronous setState here is a second render
    // before the first has painted, which is what React 19 flags.
    const check = stored
      ? checkStoredSession(() => api.me(stored, AbortSignal.timeout(ATTEMPT_TIMEOUT_MS)), {
          onWaiting: () => alive && setWaking(true),
          isAlive: () => alive,
        }).then((result) => {
          if (!alive) return;
          if (result.outcome === "invalid") {
            localStorage.removeItem(TOKEN_KEY);
            return;
          }
          setToken(stored);
          if (result.outcome === "valid") setUser(result.user);
        })
      : Promise.resolve();

    check.finally(() => {
      if (!alive) return;
      setWaking(false);
      setReady(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  const adopt = useCallback(async (accessToken: string) => {
    localStorage.setItem(TOKEN_KEY, accessToken);
    setToken(accessToken);
    setUser(await api.me(accessToken));
    setExpired(false);   // signing in successfully clears the notice
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

  const demo = useCallback(async () => {
    const { access_token } = await api.demo();
    await adopt(access_token);
  }, [adopt]);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
    setExpired(false);   // a deliberate sign-out is not an expiry
  }, []);

  const value = useMemo(
    () => ({ token, user, ready, expired, waking, signup, login, demo, logout }),
    [token, user, ready, expired, waking, signup, login, demo, logout],
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
