"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Sources } from "@/components/Sources";
import {
  api,
  askStream,
  type Conversation,
  type Message,
  type TraceStep,
} from "@/lib/api";
import { errorMessage, useAuth } from "@/lib/auth";

/**
 * A turn still in flight. It is deliberately not a `Message`: it has no id
 * because the row does not exist yet -- both messages commit together, after
 * the run -- and rendering it as one would mean inventing an id the server
 * never issued.
 */
interface Pending {
  question: string;
  answer: string;
  steps: TraceStep[];
  status: string;
}

const EXAMPLES = [
  "What is NVIDIA's gross margin?",
  "What are NVIDIA's main competitive risks?",
  "Compare NVIDIA's gross margin to Apple's.",
];

export default function ChatPage() {
  const { token, user, ready, logout } = useAuth();
  const router = useRouter();

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  // Messages are stored with the id they were loaded for, and read back only
  // when that still matches the selection. Keeping them in a bare array meant
  // clearing it in an effect on every switch, which both trips React 19's
  // set-state-in-effect rule and leaves a frame where one thread's messages
  // render under another thread's heading.
  const [thread, setThread] = useState<{ id: string | null; items: Message[] }>(
    { id: null, items: [] },
  );
  // Memoised so the scroll effect below does not see a new array every render.
  const messages = useMemo(
    () => (thread.id === activeId ? thread.items : []),
    [thread, activeId],
  );
  const [pending, setPending] = useState<Pending | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const busy = pending !== null;

  useEffect(() => {
    if (ready && !token) router.replace("/login");
  }, [ready, token, router]);

  const refreshConversations = useCallback(async () => {
    if (!token) return [];
    const list = await api.listConversations(token);
    setConversations(list);
    return list;
  }, [token]);

  // Fetched inline rather than through refreshConversations(): calling a
  // setState-wrapping helper straight from an effect body is the same cascading
  // render the rule above is about. The helper stays, for event handlers.
  useEffect(() => {
    if (!token) return;
    let alive = true;
    api
      .listConversations(token)
      .then((list) => alive && setConversations(list))
      .catch((caught) => alive && setError(errorMessage(caught)));
    return () => {
      alive = false;
    };
  }, [token]);

  // Load a thread's history whenever the selection changes.
  useEffect(() => {
    if (!token || !activeId) return;
    let current = true;
    api
      .listMessages(token, activeId)
      .then((page) => {
        // The request may have been superseded by another click while it was
        // in flight; dropping a stale response keeps the pane matching the
        // selection rather than the slowest network call.
        if (current) setThread({ id: activeId, items: page.messages });
      })
      .catch((caught) => current && setError(errorMessage(caught)));
    return () => {
      current = false;
    };
  }, [token, activeId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);

  async function newConversation() {
    if (!token) return;
    setError(null);
    try {
      const conversation = await api.createConversation(token);
      setConversations((prev) => [conversation, ...prev]);
      setActiveId(conversation.id);
      setThread({ id: conversation.id, items: [] });
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  async function send(question: string) {
    if (!token || !question.trim() || busy) return;
    setError(null);

    // A thread is created on demand, so the first question does not require
    // clicking "New conversation" first.
    let conversationId = activeId;
    if (!conversationId) {
      try {
        const conversation = await api.createConversation(token);
        setConversations((prev) => [conversation, ...prev]);
        conversationId = conversation.id;
        setActiveId(conversation.id);
      } catch (caught) {
        setError(errorMessage(caught));
        return;
      }
    }

    // The question appears immediately; the answer fills in as it streams.
    setPending({ question, answer: "", steps: [], status: "Thinking…" });
    setDraft("");

    try {
      for await (const event of askStream(token, conversationId, question)) {
        switch (event.type) {
          case "iteration":
            setPending((p) =>
              p ? { ...p, status: `Thinking… (step ${event.iteration})` } : p,
            );
            break;

          case "tool_start":
            setPending((p) =>
              p ? { ...p, status: `Running ${event.tools.join(", ")}…` } : p,
            );
            break;

          case "tool_result":
            setPending((p) =>
              p
                ? {
                    ...p,
                    status: "Reading results…",
                    steps: [
                      ...p.steps,
                      {
                        iteration: event.iteration,
                        tool: event.tool,
                        arguments: event.arguments,
                        result: event.result,
                        latency_ms: event.latency_ms,
                        model_latency_ms: 0,
                        ok: event.ok,
                      },
                    ],
                  }
                : p,
            );
            break;

          case "token":
            setPending((p) =>
              p ? { ...p, answer: p.answer + event.text, status: "" } : p,
            );
            break;

          case "discarded":
            // Those tokens are already on screen and are not the answer. The
            // server rejected the draft for stating a figure no tool produced,
            // so the text has to go, not just be appended to.
            setPending((p) =>
              p
                ? {
                    ...p,
                    answer: "",
                    status: "Rejected an unsourced draft; checking with a tool…",
                  }
                : p,
            );
            break;

          case "budget":
            setPending((p) =>
              p
                ? {
                    ...p,
                    status: `Asked for ${event.requested} tool calls; the limit is ${event.budget}.`,
                  }
                : p,
            );
            break;

          case "done": {
            // Swap the in-flight turn for the rows the server actually stored,
            // so what is on screen is what a reload would show.
            const stored = await api.listMessages(token, conversationId);
            setThread({ id: conversationId, items: stored.messages });
            setPending(null);
            refreshConversations().catch(() => {});
            break;
          }

          case "error":
            setError(`${event.error}: ${event.message}`);
            setPending(null);
            break;
        }
      }
    } catch (caught) {
      setError(errorMessage(caught));
      setPending(null);
    }
  }

  if (!ready || !token) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-neutral-500">
        Loading…
      </main>
    );
  }

  return (
    <div className="flex h-screen">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        email={user?.email ?? null}
        busy={busy}
        onSelect={setActiveId}
        onNew={newConversation}
        onLogout={logout}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl px-6 py-8">
            {messages.length === 0 && !pending && (
              <div className="mt-16 text-center">
                <h1 className="text-lg font-medium">
                  Ask about a public company
                </h1>
                <p className="mt-1 text-sm text-neutral-500">
                  Answers come from SEC filings and live tools, and every one
                  shows its sources.
                </p>
                <div className="mt-6 flex flex-col items-center gap-2">
                  {EXAMPLES.map((example) => (
                    <button
                      key={example}
                      onClick={() => send(example)}
                      className="rounded-md border border-neutral-800 px-3 py-1.5 text-sm text-neutral-400 hover:border-neutral-700 hover:text-neutral-200"
                    >
                      {example}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <ul className="space-y-6">
              {messages.map((message) => (
                <li key={message.id}>
                  <Bubble role={message.role}>
                    <p className="whitespace-pre-wrap">{message.content}</p>
                    {message.role === "assistant" && (
                      <>
                        {message.meta?.agent?.completed === false && (
                          <IncompleteNote
                            reason={message.meta.agent.stop_reason}
                          />
                        )}
                        <Sources steps={message.meta?.trace ?? []} />
                      </>
                    )}
                  </Bubble>
                </li>
              ))}

              {pending && (
                <>
                  <li>
                    <Bubble role="user">
                      <p className="whitespace-pre-wrap">{pending.question}</p>
                    </Bubble>
                  </li>
                  <li>
                    <Bubble role="assistant">
                      {pending.answer ? (
                        <p className="whitespace-pre-wrap">
                          {pending.answer}
                          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-neutral-500 align-text-bottom" />
                        </p>
                      ) : (
                        <p className="text-sm text-neutral-500">
                          {pending.status || "…"}
                        </p>
                      )}
                      {pending.answer && pending.status && (
                        <p className="mt-2 text-xs text-neutral-600">
                          {pending.status}
                        </p>
                      )}
                      {pending.steps.length > 0 && (
                        <Sources steps={pending.steps} />
                      )}
                    </Bubble>
                  </li>
                </>
              )}
            </ul>

            <div ref={bottomRef} />
          </div>
        </div>

        {error && (
          <div
            role="alert"
            className="mx-auto w-full max-w-3xl px-6 pb-2 text-xs text-red-400"
          >
            {error}
          </div>
        )}

        <form
          className="border-t border-neutral-800 px-6 py-4"
          onSubmit={(event) => {
            event.preventDefault();
            send(draft);
          }}
        >
          <div className="mx-auto flex max-w-3xl gap-2">
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={busy}
              placeholder={
                busy ? "Waiting for the answer…" : "Ask a question…"
              }
              maxLength={8000}
              className="flex-1 rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-600 disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={busy || !draft.trim()}
              className="rounded-md bg-neutral-100 px-4 py-2 text-sm font-medium text-neutral-900 disabled:opacity-40"
            >
              Send
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}

function Bubble({
  role,
  children,
}: {
  role: Message["role"];
  children: React.ReactNode;
}) {
  const isUser = role === "user";
  return (
    <div className={isUser ? "flex justify-end" : ""}>
      <div
        className={
          isUser
            ? "max-w-[80%] rounded-lg bg-neutral-800 px-3.5 py-2 text-sm"
            : "max-w-full text-sm leading-relaxed text-neutral-200"
        }
      >
        {children}
      </div>
    </div>
  );
}

/** An answer the loop did not finish is not the same as one it did. */
function IncompleteNote({ reason }: { reason: string }) {
  const explanation =
    reason === "tool_call_budget"
      ? "Stopped at the per-step tool-call budget."
      : reason === "max_iterations"
        ? "Ran out of steps before finishing."
        : reason === "inference_error"
          ? "The model could not be reached."
          : reason;
  return (
    <p className="mt-2 rounded border border-amber-900/50 bg-amber-950/30 px-2.5 py-1.5 text-xs text-amber-300/90">
      Incomplete · {explanation}
    </p>
  );
}
