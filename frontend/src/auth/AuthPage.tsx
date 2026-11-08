import { type FormEvent, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { errorMessage } from "../api/client";
import { Logo } from "../brand/Logo";
import { useAuth } from "./AuthContext";

/** Login and registration share one form; `mode` picks the endpoint. */
export default function AuthPage({ mode }: { mode: "login" | "register" }) {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = (location.state as { from?: string } | null)?.from ?? "/plans";

  if (auth.user) return <Navigate to={next} replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") await auth.login(username.trim(), password);
      else await auth.register(username.trim(), password);
      navigate(next, { replace: true });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const isLogin = mode === "login";
  return (
    <main className="auth">
      <div className="card auth-card">
        <div className="auth-head">
          <Logo />
          <span className="sheet-tag">{isLogin ? "A-002" : "A-003"}</span>
        </div>
        <p className="muted">Don't just see the floor plan. Walk it.</p>
        <h1 className="auth-title">{isLogin ? "Log in" : "Create an account"}</h1>
        <form onSubmit={submit} className="stack">
          <label>
            Username
            <input
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              minLength={3}
              maxLength={64}
              pattern="[A-Za-z0-9_.@\-]+"
              title="3–64 characters: letters, digits, _ . @ -"
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete={isLogin ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={8}
              maxLength={128}
              required
            />
          </label>
          {!isLogin && <p className="hint">At least 8 characters.</p>}
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary" disabled={busy}>
            {busy ? "Please wait…" : isLogin ? "Log in" : "Create account"}
          </button>
        </form>
        <p className="muted small">
          {isLogin ? (
            <>
              No account? <Link to="/register" state={location.state}>Create one</Link>
            </>
          ) : (
            <>
              Already registered? <Link to="/login" state={location.state}>Log in</Link>
            </>
          )}
        </p>
        <p className="muted small">
          Just looking? <a href="/viewer?fixture=call-center-offices">Walk a sample office</a> without an account.
        </p>
      </div>
    </main>
  );
}
