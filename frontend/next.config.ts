import type { NextConfig } from "next";

// The Python FastAPI server is the backend. Locally every /api request (including the live-voice WebSocket,
// which Next.js proxies as an upgrade) is forwarded to it, so the browser only ever talks to this origin.
// The port is read at build time; ui.py rebuilds when it changes.
// Hosted (NEXT_PUBLIC_API_URL set, e.g. on Vercel), the browser calls the API directly: no forwarding.
const apiPort = process.env.VOICE_TUTOR_API_PORT || "8766";
const apiHost = process.env.VOICE_TUTOR_HOST || "127.0.0.1";
const hosted = Boolean(process.env.NEXT_PUBLIC_API_URL);

// Security headers on every response (the Content-Security-Policy is per request, in src/proxy.ts).
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "microphone=(self), camera=(), geolocation=(), payment=()" },
  ...(hosted ? [{ key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" }] : []),
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async rewrites() {
    if (hosted) return [];
    return [{ source: "/api/:path*", destination: `http://${apiHost}:${apiPort}/api/:path*` }];
  },
  experimental: {
    // Proxied requests default to a 30 s timeout and a 10 MB buffered body. PDF uploads can be up to
    // 200 MB each (several at once), and the first request after start may wait for the embedding model.
    proxyTimeout: 10 * 60 * 1000,
    proxyClientMaxBodySize: "1gb",
  },
};

export default nextConfig;
