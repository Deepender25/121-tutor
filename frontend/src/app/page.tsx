import { redirect } from "next/navigation";
import { HomeView } from "@/components/home/HomeView";
import { ServerOffline } from "@/components/RouteStates";
import { loadNotebooks } from "@/lib/server-api";

export default async function HomePage() {
  const result = await loadNotebooks();
  if (result.kind === "signin") redirect("/login");
  if (result.kind !== "ok") return <ServerOffline />;
  return <HomeView initialNotebooks={result.data} />;
}
