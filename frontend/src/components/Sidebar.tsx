"use client";

import Link from "next/link";
import type { Conversation } from "@/lib/api";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  email: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  /** Unused: signing out is in the site header. */
  onLogout?: () => void;
}

/** Newest activity first, matching what the API already sorts by. */
export function Sidebar({
  conversations,
  activeId,
  email,
  busy,
  onSelect,
  onNew,
}: Props) {
  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-border bg-bg md:flex">
      <div className="p-3">
        <button
          onClick={onNew}
          disabled={busy}
          className="w-full rounded-md border border-fg-subtle px-3 py-2 text-sm hover:bg-surface-2 disabled:opacity-40"
        >
          + New conversation
        </button>
        <Link
          href="/chart"
          className="mt-2 block w-full rounded-md px-3 py-1.5 text-center text-xs text-fg-muted hover:bg-surface-2"
        >
          Price charts
        </Link>
        <Link
          href="/backtest"
          className="block w-full rounded-md px-3 py-1.5 text-center text-xs text-fg-muted hover:bg-surface-2"
        >
          Backtests
        </Link>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2">
        {conversations.length === 0 ? (
          <p className="px-2 py-4 text-xs text-fg-subtle">
            No conversations yet.
          </p>
        ) : (
          <ul className="space-y-0.5 pb-3">
            {conversations.map((conversation) => (
              <li key={conversation.id}>
                <button
                  onClick={() => onSelect(conversation.id)}
                  // A run in flight is bound to one conversation; switching
                  // mid-stream would leave its tokens landing in a thread the
                  // user is no longer looking at.
                  disabled={busy}
                  aria-current={conversation.id === activeId ? "true" : undefined}
                  className={`w-full truncate rounded px-2 py-1.5 text-left text-sm disabled:opacity-40 ${
                    conversation.id === activeId
                      ? "bg-surface-2 text-fg"
                      : "text-fg-muted hover:bg-surface-2"
                  }`}
                  title={conversation.title ?? "Untitled"}
                >
                  {conversation.title ?? (
                    <span className="italic text-fg-subtle">Untitled</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </nav>

      <div className="border-t border-border p-3">
        <p className="truncate text-xs text-fg-subtle" title={email ?? ""}>
          {email}
        </p>
      </div>
    </aside>
  );
}
