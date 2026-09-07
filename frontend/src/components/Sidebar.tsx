"use client";

import type { Conversation } from "@/lib/api";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  email: string | null;
  busy: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onLogout: () => void;
}

/** Newest activity first, matching what the API already sorts by. */
export function Sidebar({
  conversations,
  activeId,
  email,
  busy,
  onSelect,
  onNew,
  onLogout,
}: Props) {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-neutral-800 bg-neutral-950">
      <div className="p-3">
        <button
          onClick={onNew}
          disabled={busy}
          className="w-full rounded-md border border-neutral-700 px-3 py-2 text-sm hover:bg-neutral-900 disabled:opacity-40"
        >
          + New conversation
        </button>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-2">
        {conversations.length === 0 ? (
          <p className="px-2 py-4 text-xs text-neutral-600">
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
                      ? "bg-neutral-800 text-neutral-100"
                      : "text-neutral-400 hover:bg-neutral-900"
                  }`}
                  title={conversation.title ?? "Untitled"}
                >
                  {conversation.title ?? (
                    <span className="italic text-neutral-600">Untitled</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </nav>

      <div className="border-t border-neutral-800 p-3">
        <p className="truncate text-xs text-neutral-500" title={email ?? ""}>
          {email}
        </p>
        <button
          onClick={onLogout}
          className="mt-1.5 text-xs text-neutral-500 underline hover:text-neutral-300"
        >
          Sign out
        </button>
      </div>
    </aside>
  );
}
