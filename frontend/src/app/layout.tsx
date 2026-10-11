import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource-variable/fraunces/opsz.css";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { THEME_SCRIPT } from "@/lib/theme";

export const metadata: Metadata = {
  title: { default: "Voice Tutor", template: "%s · Voice Tutor" },
  description: "Talk one-to-one with your documents: tutor, explainer, researcher, brainstormer.",
  icons: { icon: { url: "/favicon.svg", type: "image/svg+xml" } },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f3ef" },
    { media: "(prefers-color-scheme: dark)", color: "#0d0d0f" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const nonce = (await headers()).get("x-nonce") ?? undefined; // set by proxy.ts for the CSP
  return (
    // The inline script sets data-theme before first paint; React must not fight it on hydration.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <div id="app">{children}</div>
        <AppShell />
      </body>
    </html>
  );
}
