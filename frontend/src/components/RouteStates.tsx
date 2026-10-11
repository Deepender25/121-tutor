"use client";
// Full-page states shared by routes: server offline (with retry), not found, unexpected error.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTransition } from "react";
import { Icon } from "./Icon";

export function ServerOffline() {
  const router = useRouter();
  const [pending, start] = useTransition();
  return (
    <div className="empty-hero">
      <div className="modal-icon danger"><Icon name="alert" /></div>
      <h2>Can&apos;t reach the Voice Tutor server</h2>
      <p>The “Voice Tutor - API” window may be closed or still starting. Start the app with run.bat, then try again.</p>
      <button className="btn primary" disabled={pending} onClick={() => start(() => router.refresh())}>
        {pending ? "Checking…" : "Try again"}
      </button>
    </div>
  );
}

export function NotFoundState({ title, body }: { title: string; body: string }) {
  return (
    <div className="empty-hero">
      <div className="modal-icon"><Icon name="info" /></div>
      <h2>{title}</h2>
      <p>{body}</p>
      <Link className="btn primary" href="/">Back to notebooks</Link>
    </div>
  );
}

export function ErrorState({ retry }: { retry: () => void }) {
  return (
    <div className="empty-hero">
      <div className="modal-icon danger"><Icon name="alert" /></div>
      <h2>Something went wrong</h2>
      <p>This page hit an unexpected error. Details are in the browser console and logs\ui.log.</p>
      <div className="hero-actions" style={{ justifyContent: "center" }}>
        <button className="btn primary" onClick={() => retry()}>Try again</button>
        <Link className="btn" href="/">Back to notebooks</Link>
      </div>
    </div>
  );
}
