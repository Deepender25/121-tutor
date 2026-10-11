import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { NotebookView } from "@/components/notebook/NotebookView";
import { ServerOffline } from "@/components/RouteStates";
import { loadNotebook } from "@/lib/server-api";

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const { id } = await params;
  const result = await loadNotebook(id);
  if (result.kind === "ok") return { title: result.data.name };
  return { title: result.kind === "missing" ? "Notebook not found" : result.kind === "signin" ? "Sign in" : "Voice Tutor" };
}

export default async function NotebookPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await loadNotebook(id);
  if (result.kind === "signin") redirect(`/login?next=${encodeURIComponent(`/notebooks/${id}`)}`);
  if (result.kind === "missing") notFound();
  if (result.kind === "offline") return <ServerOffline />;
  // key: a different notebook gets a fresh view (its own voice session, viewer, state).
  return <NotebookView key={result.data.id} initial={result.data} />;
}
