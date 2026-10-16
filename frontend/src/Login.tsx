// Login screen (Phase 14a, share gate). Rendered by App INSTEAD of the app
// shell while locked — no header, no ticker, no dashboards; nothing here
// fetches anything except the login POST itself. Dark TUI card, no browser
// dialogs (window.prompt/alert/confirm).
import { useState, type FormEvent } from "react";

import { ApiError, login } from "./api/client";

export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [passphrase, setPassphrase] = useState("");
  // Defaults to off: the low-effort path (type passphrase, hit Enter) should
  // issue a session cookie, not a 30-day one on someone else's device.
  const [remember, setRemember] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || !passphrase) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(passphrase, remember);
      onSuccess();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setError("Wrong passphrase");
      else if (err instanceof ApiError && err.status === 429) setError("Too many attempts. Wait a few minutes.");
      else setError("Could not reach the server. Try again.");
      setSubmitting(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit} aria-label="Log in to Catallax">
        <div>
          <h1 className="login-wordmark">C A T A L L A X</h1>
          <div className="login-tagline">personal financial terminal</div>
        </div>

        <div>
          <label className="login-label" htmlFor="login-passphrase">
            passphrase
          </label>
          <input
            id="login-passphrase"
            className="login-input"
            style={{ marginTop: 5 }}
            type="password"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            aria-label="passphrase"
            autoFocus
            autoComplete="current-password"
            disabled={submitting}
          />
        </div>

        <label className="login-remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            disabled={submitting}
          />
          remember this device
        </label>

        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}

        <button type="submit" className="login-submit" disabled={submitting || !passphrase}>
          ENTER
        </button>
      </form>
    </div>
  );
}
