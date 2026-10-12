// Keep a phone's screen on while the app is open and visible (Screen Wake Lock API): a lesson is listened to,
// not tapped through, and a sleeping screen would cut the conversation off. Phones and tablets only (coarse
// pointer); desktops keep their own sleep settings. The browser drops the lock whenever the page is hidden,
// so it's taken again on return; some browsers only grant it after a tap, so a tap retries too.
type Sentinel = { released: boolean; release(): Promise<void>; addEventListener(type: "release", fn: () => void): void };
type WakeLockApi = { request(type: "screen"): Promise<Sentinel> };

// What happened to the last request, for the voice session's diagnostics report.
let status = "not requested";
export const wakeLockStatus = () => status;

export function keepScreenOn(): () => void {
  const wakeLock = (navigator as Navigator & { wakeLock?: WakeLockApi }).wakeLock;
  if (!matchMedia("(pointer: coarse)").matches) { status = "not a touch device"; return () => {}; }
  if (!wakeLock) { status = "unsupported"; return () => {}; }
  let lock: Sentinel | null = null;
  let pending = false;
  let stopped = false;

  const acquire = async () => {
    if (stopped || pending || (lock && !lock.released) || document.visibilityState !== "visible") return;
    pending = true;
    try {
      lock = await wakeLock.request("screen");
      status = "held";
      lock.addEventListener("release", () => { lock = null; status = "released"; });
      if (stopped) lock.release().catch(() => {});
    } catch (e) { // not allowed right now (battery saver, no tap yet): the next visibility change or tap retries
      status = `refused (${(e as Error)?.name || "error"})`;
    }
    finally { pending = false; }
  };
  const onVisible = () => { if (document.visibilityState === "visible") acquire(); };

  acquire();
  document.addEventListener("visibilitychange", onVisible);
  document.addEventListener("pointerdown", acquire, { passive: true });
  return () => {
    stopped = true;
    document.removeEventListener("visibilitychange", onVisible);
    document.removeEventListener("pointerdown", acquire);
    lock?.release().catch(() => {});
  };
}
