import type { Metadata } from "next";
import { LoginView } from "@/components/LoginView";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string | string[] }> }) {
  const { next } = await searchParams;
  return <LoginView next={typeof next === "string" ? next : null} />;
}
