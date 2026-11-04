import type { ReactNode } from 'react';

export function PanelSection({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="hair border-t px-5 py-5 first:border-t-0">
      <div className="t-label mb-3 flex items-baseline justify-between gap-3">
        <span>{title}</span>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function PanelRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[100px_1fr] gap-3 py-1 text-[12.5px]">
      <dt className="t-faint">{term}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** Narrow, searchable list column used by the Pathology and 3D/4D workspaces. */
export function ListColumn({
  query,
  onQuery,
  placeholder,
  count,
  children,
  collapsed = false,
  onToggle,
  rail,
}: {
  query: string;
  onQuery: (q: string) => void;
  placeholder: string;
  count: string;
  children: ReactNode;
  collapsed?: boolean;
  onToggle?: () => void;
  /** Compact content shown when the column is collapsed. */
  rail?: ReactNode;
}) {
  if (collapsed) {
    return (
      <aside className="hair flex w-[52px] shrink-0 flex-col items-center border-r">
        <button className="list-toggle mt-3" onClick={onToggle} aria-label="Show the patient list" title="Show the patient list">
          ›
        </button>
        <div className="mt-2 min-h-0 w-full flex-1 overflow-y-auto pb-3">{rail}</div>
      </aside>
    );
  }
  return (
    <aside className="hair flex w-[300px] shrink-0 flex-col border-r">
      <div className="hair border-b p-3">
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder={placeholder}
            aria-label={placeholder}
            className="field"
          />
          {onToggle && (
            <button className="list-toggle" onClick={onToggle} aria-label="Hide the patient list" title="Hide the patient list">
              ‹
            </button>
          )}
        </div>
        <div className="t-faint mt-2 text-[11.5px]">{count}</div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto py-1">{children}</div>
    </aside>
  );
}

export function WorkspaceHeader({
  title,
  meta,
  description,
  explain,
  aside,
}: {
  title: string;
  meta: ReactNode;
  description: string;
  /** Plain-language explanation shown below the description. */
  explain?: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <div className="hair flex shrink-0 items-start gap-8 border-b px-5 pb-4 pt-4">
      <div className="min-w-0 flex-1">
        <h1 className="t-display text-[28px]">{title}</h1>
        <div className="t-faint mt-1 text-[12.5px]">{meta}</div>
        <p className="t-muted mt-2 max-w-[110ch] text-[13.5px]">{description}</p>
        {explain}
      </div>
      {aside}
    </div>
  );
}
