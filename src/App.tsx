import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { AppShell } from '@/components/AppShell';
import { AuthPage } from '@/components/AuthPage';
import { useAuth } from '@/hooks/AuthContext';
import { AuthCallback } from '@/pages/AuthCallback.tsx';
import { DataMap } from '@/pages/DataMap';
import { Patients } from '@/pages/Patients';
import { Sources } from '@/pages/Sources';

/** The route a visitor asked for before being sent to sign in. */
interface AuthRedirectState {
  from?: string;
}

/**
 * Resolves where to send a visitor after they sign in.
 *
 * Only same-origin application paths are accepted, so a crafted value cannot
 * send the visitor off-site, and the sign-in routes are rejected so sign-in
 * cannot loop.
 */
function resolveReturnPath(state: unknown): string {
  const from = (state as AuthRedirectState | null)?.from;

  if (typeof from !== 'string' || !from.startsWith('/')) return '/';
  if (from.startsWith('//') || from.startsWith('/\\')) return '/';
  if (from === '/auth' || from.startsWith('/auth/') || from.startsWith('/auth?')) return '/';

  return from;
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="t-faint">Loading</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    const redirectState: AuthRedirectState = { from: `${location.pathname}${location.search}` };
    return <Navigate to="/auth" replace state={redirectState} />;
  }

  return <>{children}</>;
}

function PublicRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="t-faint">Loading</div>
      </div>
    );
  }

  if (isAuthenticated) return <Navigate to={resolveReturnPath(location.state)} replace />;

  return <>{children}</>;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallback />} />
        <Route
          path="/auth"
          element={
            <PublicRoute>
              <AuthPage />
            </PublicRoute>
          }
        />
        <Route
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          <Route index element={<Patients />} />
          <Route path="p/:id" element={<Patients />} />
          <Route path="data" element={<DataMap />} />
          <Route path="sources" element={<Sources />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
