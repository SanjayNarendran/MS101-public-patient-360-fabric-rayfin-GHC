import { Link, Outlet, useLocation } from 'react-router-dom';

import { ThemeToggle } from '@/components/ThemeToggle';
import { Wordmark } from '@/components/Wordmark';
import { useAuth } from '@/hooks/AuthContext';
import { CohortProvider } from '@/hooks/useCohort';
import { LOCAL_DATA } from '@/lib/localMode';

const TABS = [
  { to: '/', label: 'Patients', active: (p: string) => p === '/' || p.startsWith('/p/') },
  { to: '/data', label: 'Where the data lives', active: (p: string) => p.startsWith('/data') },
  { to: '/sources', label: 'Sources', active: (p: string) => p.startsWith('/sources') },
];

// Written to .env.production.local by scripts/setup_fabric.py; the link is left out when unset.
const WORKSPACE_ID = import.meta.env.VITE_FABRIC_WORKSPACE_ID as string | undefined;
const WORKSPACE_NAME = (import.meta.env.VITE_FABRIC_WORKSPACE_NAME as string | undefined) ?? 'Fabric workspace';

function ShellHeader() {
  const { user, signOut } = useAuth();
  const { pathname } = useLocation();
  return (
    <header className="hair shrink-0 border-b">
      <div className="flex h-14 items-center gap-8 px-5">
        <Wordmark compact />
        <nav className="flex h-full items-stretch gap-6" aria-label="Sections">
          {TABS.map((t) => (
            <Link key={t.to} to={t.to} className={`tab ${t.active(pathname) ? 'active' : ''}`}>
              {t.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-5 text-[12.5px]">
          {LOCAL_DATA ? (
            <span className="t-muted t-mono hidden xl:inline" title="Local mode: data is read from the work/ folder on this computer">
              local mode · work/
            </span>
          ) : (
            WORKSPACE_ID && (
              <a
                href={`https://app.fabric.microsoft.com/groups/${WORKSPACE_ID}`}
                target="_blank"
                rel="noreferrer"
                className="link t-muted t-mono hidden xl:inline"
              >
                {WORKSPACE_NAME}
              </a>
            )
          )}
          {user && !LOCAL_DATA && (
            <>
              <span className="t-faint hidden 2xl:inline">{user.email || user.name}</span>
              <button onClick={() => void signOut()} className="link t-muted">
                Sign out
              </button>
            </>
          )}
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

export function AppShell() {
  return (
    <CohortProvider>
      <div className="flex h-screen flex-col">
        <ShellHeader />
        <div className="min-h-0 flex-1">
          <Outlet />
        </div>
      </div>
    </CohortProvider>
  );
}
