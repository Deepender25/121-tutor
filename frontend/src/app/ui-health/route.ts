// Liveness check for the launcher (ui.py / run.bat): is this the Voice Tutor UI server?
export function GET() {
  return Response.json({ ok: true, app: "voice-tutor-ui" }, { headers: { "Cache-Control": "no-store" } });
}
