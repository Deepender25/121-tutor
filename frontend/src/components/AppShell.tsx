"use client";
// Client-side app chrome shared by every page: overlay hosts, server status polling, closing dialogs on
// navigation, and redirecting links from the old single-page app (/#/nb/<id>) to real routes.
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { startStatusPolling } from "@/lib/status";
import { closeAllOverlays, OverlayHosts } from "./overlays";

export function AppShell() {
  const pathname = usePathname();
  const router = useRouter();

  // Not on the sign-in page: there's no session yet to ask with (signing in reloads the page, which starts it).
  useEffect(() => { if (!location.pathname.startsWith("/login")) startStatusPolling(); }, []);

  // Dialogs and menus belong to the page they were opened on.
  useEffect(() => { closeAllOverlays(); }, [pathname]);

  useEffect(() => {
    const legacy = location.hash.match(/^#\/nb\/([a-z0-9]+)/i);
    if (legacy) router.replace(`/notebooks/${legacy[1]}`);
  }, [router]);

  return <OverlayHosts />;
}
