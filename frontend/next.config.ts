import type { NextConfig } from "next";

// The Python FastAPI server is the backend. Every /api request (including the live-voice WebSocket,
// which Next.js proxies as an upgrade) is forwarded to it, so the browser only ever talks to this origin.
// The port is read at build time; ui.py rebuilds when it changes.
const apiPort = process.env.VOICE_TUTOR_API_PORT || "8766";
const apiHost = process.env.VOICE_TUTOR_HOST || "127.0.0.1";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
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
