"use client";

/**
 * An answer's markdown, rendered safely.
 *
 * The model writes markdown (**bold**, lists, tables), and the chat showed the
 * asterisks. This renders it with react-markdown (MIT), which builds React
 * elements from a syntax tree and never sets innerHTML:
 *
 * - `skipHtml`: raw HTML in an answer is dropped, not rendered. A filing
 *   excerpt or a news headline the model quotes is not trusted markup.
 * - links go through react-markdown's default URL filter, which removes
 *   `javascript:` and other unsafe protocols, and open in a new tab.
 * - remark-gfm adds tables and strikethrough, which the model uses.
 */

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-2 list-disc pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal pl-5">{children}</ol>,
  li: ({ children }) => <li className="mb-0.5">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-neutral-50">{children}</strong>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer noopener" className="underline">
      {children}
    </a>
  ),
  code: ({ children }) => (
    <code className="rounded bg-neutral-800 px-1 py-0.5 text-[0.9em]">{children}</code>
  ),
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto">
      <table className="text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-neutral-700 px-2 py-1 text-left font-medium">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-neutral-800 px-2 py-1">{children}</td>,
  h1: ({ children }) => <p className="mb-2 font-semibold">{children}</p>,
  h2: ({ children }) => <p className="mb-2 font-semibold">{children}</p>,
  h3: ({ children }) => <p className="mb-2 font-semibold">{children}</p>,
};

export function Markdown({ text }: { text: string }) {
  return (
    <div className="break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
