"use client";
// Sign-in page for the hosted app (the server has VOICE_TUTOR_AUTH=on). Locally login is off and this page just
// goes back home. After signing in it does a full navigation, so server-rendered pages see the new session.
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, safeNext } from "@/lib/api";
import { Icon, Mark } from "./Icon";

export function LoginView({ next }: { next: string | null }) {
  const router = useRouter();
  const target = safeNext(next);
  const [checking, setChecking] = useState(true);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.me()
      .then((me) => {
        if (!live) return;
        if (me.auth === "off") router.replace("/");
        else location.replace(target); // already signed in
      })
      .catch(() => { if (live) setChecking(false); }); // 401: show the form
    return () => { live = false; };
  }, [router, target]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.login(email.trim(), password);
      location.assign(target);
    } catch (err) {
      setError((err as Error).message);
      setPassword("");
      setBusy(false);
    }
  };

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit} aria-busy={checking || busy} noValidate>
        <div className="auth-brand"><Mark className="brand-mark" /><span>Voice Tutor</span></div>
        <h1>Sign in</h1>
        <p className="auth-sub">Use the account set up for this server.</p>
        {error ? <div className="banner error" role="alert"><Icon name="alert" /><div>{error}</div></div> : null}
        <label className="auth-label" htmlFor="email">Email</label>
        <input id="email" className="field" type="email" autoComplete="username" inputMode="email" required
          autoFocus value={email} onChange={(e) => setEmail(e.target.value)} disabled={checking || busy} maxLength={320} />
        <label className="auth-label" htmlFor="password">Password</label>
        <input id="password" className="field" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} disabled={checking || busy} maxLength={128} />
        <button className="btn primary lg auth-submit" type="submit" disabled={checking || busy || !email.trim() || !password}>
          {busy ? "Signing in…" : checking ? "Checking…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
