import { lazy, type ReactNode, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import AuthPage from "./auth/AuthPage";
import LandingPage from "./landing/LandingPage";
import PlanListPage from "./plans/PlanListPage";

// Babylon.js is large: the 3D pages load only when opened.
const PlanPage = lazy(() => import("./plans/PlanPage"));
const FixtureViewer = lazy(() => import("./viewer/FixtureViewer"));

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Suspense fallback={<p className="loading">Loading 3D viewer…</p>}>
          {/* Unknown paths go to the landing page; signed-in users reach their plans from there. */}
          <Routes>
            <Route path="/" element={<LandingPage />} />
            <Route path="/login" element={<AuthPage mode="login" />} />
            <Route path="/register" element={<AuthPage mode="register" />} />
            <Route path="/viewer" element={<FixtureViewer />} />
            <Route path="/plans" element={<RequireAuth><PlanListPage /></RequireAuth>} />
            <Route path="/plans/:id" element={<RequireAuth><PlanPage /></RequireAuth>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <p className="loading">Signing you in…</p>;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}
