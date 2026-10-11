// Content Security Policy for every page: the browser may only run this app's own scripts (plus inline ones that
// carry this request's nonce, which Next.js adds to its own), and may only connect to this site and the API.
// Injected HTML can't run code or send your data elsewhere. Pages render per request, so each gets a new nonce.
// No 'strict-dynamic': it would also stop 'self' from covering the microphone's audio worklet. Styles need
// 'unsafe-inline' for React's style attributes (and a style nonce would switch that off).
import { NextResponse, type NextRequest } from "next/server";

const API_ORIGIN = process.env.NEXT_PUBLIC_API_URL ? new URL(process.env.NEXT_PUBLIC_API_URL).origin : "";
const API_SOURCES = API_ORIGIN ? ` ${API_ORIGIN} ${API_ORIGIN.replace(/^http/, "ws")}` : "";

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const dev = process.env.NODE_ENV === "development"; // React's dev tooling needs eval
  const api = API_ORIGIN ? ` ${API_ORIGIN}` : "";
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' data: blob:${api}`, // page images come from the API when hosted
    "font-src 'self' data:",
    `connect-src 'self'${API_SOURCES}`, // REST + the voice WebSocket
    `media-src 'self' blob:${api}`, // voice previews
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // pages only: not /api (proxied to the Python server locally), built assets or public files
      source: "/((?!api/|_next/static|_next/image|favicon.svg|brand/|audio/).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
