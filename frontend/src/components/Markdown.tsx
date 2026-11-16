"use client";

// Answers are written in markdown. Rendered, not shown raw (the literal "**"
// bug), and safely: `skipHtml` drops any raw HTML in the text instead of
// rendering it, so a model or a quoted filing cannot inject markup. Links open
// in a new tab without passing the opener.
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed text-fg [&_li]:my-1">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener" className="text-accent underline">
              {children}
            </a>
          ),
          strong: ({ children }) => <strong className="font-semibold text-fg">{children}</strong>,
          ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
          h1: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
          h2: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
          h3: ({ children }) => <h4 className="font-semibold">{children}</h4>,
          code: ({ children }) => (
            <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-left text-sm">{children}</table>
            </div>
          ),
          th: ({ children }) => <th className="border-b border-border px-2 py-1 font-medium">{children}</th>,
          td: ({ children }) => <td className="border-b border-border/60 px-2 py-1">{children}</td>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
