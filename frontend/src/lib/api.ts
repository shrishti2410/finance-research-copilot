/**
 * The one place that knows how to talk to the FastAPI backend.
 *
 * Every call goes through `request`, so auth, error shape, and the base URL are
 * decided once. Components get typed data or a thrown `ApiError` -- never a
 * Response they have to remember to check `.ok` on.
 */

import { sessionExpired } from "./session";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** FastAPI returns `detail` as a string, or as a list for 422s. */
function readDetail(body: unknown, fallback: string): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string; loc?: string[] } | undefined;
      if (first?.msg) {
        const field = first.loc?.slice(1).join(".");
        return field ? `${field}: ${first.msg}` : first.msg;
      }
    }
  }
  return fallback;
}

async function request<T>(
  path: string,
  { token, ...init }: RequestInit & { token?: string | null } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    });
  } catch {
    // fetch only rejects for network-level failures, and "the API isn't
    // running" is by far the most likely one in development.
    throw new ApiError(0, `Cannot reach the API at ${API_BASE}. Is it running?`);
  }

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // A 401 here means the token expired or was revoked. It is never a
    // per-request problem the caller can retry past, so it is handled once,
    // centrally, rather than by every call site remembering to check.
    if (response.status === 401) sessionExpired();
    throw new ApiError(
      response.status,
      readDetail(body, `${response.status} ${response.statusText}`),
    );
  }
  return body as T;
}

// ── types, mirroring api/schemas.py ─────────────────────────────────────────

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface TraceStep {
  iteration: number;
  tool: string;
  arguments: Record<string, unknown>;
  result: unknown;
  latency_ms: number;
  model_latency_ms: number;
  ok: boolean;
}

export interface MessageMeta {
  agent?: {
    model: string;
    iterations: number;
    completed: boolean;
    stop_reason: string;
    total_ms: number;
    tools_called: string[];
    history_messages: number;
  };
  trace?: TraceStep[];
}

export interface Message {
  id: number;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  meta: MessageMeta | null;
  created_at: string;
}

// ── endpoints ───────────────────────────────────────────────────────────────

export const api = {
  signup: (email: string, password: string) =>
    request<TokenResponse>("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: (token: string) => request<User>("/auth/me", { token }),

  listConversations: (token: string) =>
    request<Conversation[]>("/conversations", { token }),

  createConversation: (token: string, title: string | null = null) =>
    request<Conversation>("/conversations", {
      method: "POST",
      token,
      body: JSON.stringify({ title }),
    }),

  listMessages: (token: string, conversationId: string) =>
    request<{ messages: Message[]; next_cursor: number | null }>(
      `/conversations/${conversationId}/messages?limit=500`,
      { token },
    ),
};

// ── the streaming ask ───────────────────────────────────────────────────────

/**
 * Events emitted by POST /ask/stream, in the order they arrive.
 * Mirrors the docstring on `ask_stream` in api/routes.py.
 */
export type AskEvent =
  | { type: "iteration"; iteration: number }
  | { type: "tool_start"; iteration: number; tools: string[] }
  | {
      type: "tool_result";
      iteration: number;
      tool: string;
      arguments: Record<string, unknown>;
      ok: boolean;
      latency_ms: number;
      result: string;
    }
  | { type: "token"; text: string }
  | { type: "discarded"; iteration: number; reason: string; draft: string }
  | {
      type: "blocked";
      iteration: number;
      reason: string;
      phrases: string[];
      draft: string;
    }
  | {
      type: "budget";
      iteration: number;
      requested: number;
      budget: number;
      skipped: string[];
    }
  | {
      type: "done";
      conversation_id: string;
      user_message_id: number;
      assistant_message_id: number;
      answer: string;
      completed: boolean;
      stop_reason: string;
      iterations: number;
      model: string;
      total_ms: number;
      history_messages: number;
      steps: TraceStep[];
    }
  | { type: "error"; error: string; message: string };

/**
 * Ask a question and yield events as they arrive.
 *
 * Uses fetch + a stream reader rather than EventSource, which cannot send an
 * Authorization header. Frames are separated by a blank line; a partial frame
 * at the end of a chunk is held over rather than parsed, because a JSON payload
 * is routinely split across TCP reads.
 */
export async function* askStream(
  token: string,
  conversationId: string,
  message: string,
  signal?: AbortSignal,
): AsyncGenerator<AskEvent> {
  const response = await fetch(`${API_BASE}/ask/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId, message }),
    signal,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (response.status === 401) sessionExpired();
    throw new ApiError(
      response.status,
      readDetail(body, `${response.status} ${response.statusText}`),
    );
  }
  if (!response.body) throw new ApiError(0, "The response carried no body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            yield JSON.parse(payload) as AskEvent;
          } catch {
            // A frame we cannot parse is not worth killing the stream over;
            // the run is still going and the `done` event is what matters.
            console.warn("askStream: unparseable frame", payload.slice(0, 200));
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
