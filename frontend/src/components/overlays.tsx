"use client";
// App-wide overlays: toasts, dialogs and sheets (promise-based), and pop-up menus. They can be opened from
// anywhere (components, the voice client, timers); the hosts below render them. Same markup and classes as
// before, including the 180 ms leave animation.
import { type ReactNode, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { createStore, useStore } from "@/lib/store";
import { Icon } from "./Icon";

// ---------------------------------------------------------------- hydration

const noopSubscribe = () => () => {};
// False while server-rendering and hydrating, true after: for values that differ between server and browser
// (local times, greetings), so hydration never mismatches.
export function useHydrated(): boolean {
  return useSyncExternalStore(noopSubscribe, () => true, () => false);
}

// ---------------------------------------------------------------- toasts

type ToastKind = "" | "success" | "error" | "info";
interface ToastItem { id: number; message: string; kind: ToastKind; leaving: boolean }
const toastStore = createStore({ items: [] as ToastItem[] });
let toastSeq = 0;
const TOAST_ICON: Record<string, string> = { success: "checkCircle", error: "xCircle", info: "info", "": "info" };

export function toast(message: string, kind: ToastKind = "", ms = 4200) {
  const id = ++toastSeq;
  toastStore.set((s) => ({ items: [...s.items, { id, message, kind, leaving: false }] }));
  setTimeout(() => toastStore.set((s) => ({ items: s.items.map((t) => (t.id === id ? { ...t, leaving: true } : t)) })), ms);
  setTimeout(() => toastStore.set((s) => ({ items: s.items.filter((t) => t.id !== id) })), ms + 400);
}

export async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied", "success", 1600);
  } catch {
    toast("Couldn't copy to the clipboard", "error");
  }
}

function Toaster() {
  const items = useStore(toastStore, (s) => s.items);
  return (
    <div id="toasts" role="status">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}${t.leaving ? " leaving" : ""}`} role={t.kind === "error" ? "alert" : "status"}>
          <Icon name={TOAST_ICON[t.kind] || "info"} />
          <span>{t.message}</span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- dialogs & sheets

type Close = (value?: unknown) => void;
interface Layer {
  id: number;
  kind: "modal" | "sheet";
  render: (close: Close) => ReactNode;
  resolve: (value: unknown) => void;
  leaving: boolean;
  prevFocus: Element | null;
}
const layerStore = createStore({ layers: [] as Layer[] });
let layerSeq = 0;

function closeLayer(id: number, value?: unknown) {
  const layer = layerStore.get().layers.find((l) => l.id === id);
  if (!layer || layer.leaving) return;
  layerStore.set((s) => ({ layers: s.layers.map((l) => (l.id === id ? { ...l, leaving: true } : l)) }));
  setTimeout(() => layerStore.set((s) => ({ layers: s.layers.filter((l) => l.id !== id) })), 180);
  const prev = layer.prevFocus as HTMLElement | null;
  if (prev && prev.isConnected) prev.focus?.();
  layer.resolve(value);
}

function openLayer<T>(kind: Layer["kind"], render: (close: (value?: T) => void) => ReactNode): Promise<T | undefined> {
  return new Promise((resolve) => {
    const id = ++layerSeq;
    layerStore.set((s) => ({
      layers: [...s.layers, { id, kind, render: render as Layer["render"], resolve: resolve as Layer["resolve"], leaving: false, prevFocus: document.activeElement }],
    }));
  });
}

export const modal = <T,>(render: (close: (value?: T) => void) => ReactNode) => openLayer<T>("modal", render);
export const sheet = <T,>(render: (close: (value?: T) => void) => ReactNode) => openLayer<T>("sheet", render);

// Closes every dialog, sheet and menu (on navigation).
export function closeAllOverlays() {
  for (const l of layerStore.get().layers) closeLayer(l.id);
  menuStore.set({ menu: null });
}

export function confirmDialog({ title, body, confirm = "Delete", danger = true }: { title: string; body: string; confirm?: string; danger?: boolean }) {
  return modal<boolean>((close) => (
    <>
      <div className={`modal-icon ${danger ? "danger" : ""}`}><Icon name={danger ? "trash" : "info"} /></div>
      <h2>{title}</h2>
      <p>{body}</p>
      <div className="actions">
        <button className="btn" onClick={() => close(false)}>Cancel</button>
        <button className={`btn ${danger ? "danger" : "primary"}`} onClick={() => close(true)}>{confirm}</button>
      </div>
    </>
  )).then((v) => v === true);
}

function PromptBody({ close, title, body, value, placeholder, confirm }: {
  close: (v?: string) => void; title: string; body: string; value: string; placeholder: string; confirm: string;
}) {
  const [text, setText] = useState(value);
  const submit = () => close(text.trim() || undefined);
  return (
    <>
      <h2>{title}</h2>
      {body ? <p>{body}</p> : null}
      <input className="field" value={text} placeholder={placeholder} maxLength={120} data-autofocus=""
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
      <div className="actions">
        <button className="btn" onClick={() => close(undefined)}>Cancel</button>
        <button className="btn primary" onClick={submit}>{confirm}</button>
      </div>
    </>
  );
}

export function promptDialog({ title, body = "", value = "", placeholder = "", confirm = "Save" }: {
  title: string; body?: string; value?: string; placeholder?: string; confirm?: string;
}) {
  return modal<string>((close) => <PromptBody close={close} title={title} body={body} value={value} placeholder={placeholder} confirm={confirm} />);
}

function LayerView({ layer, top }: { layer: Layer; top: boolean }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const [labelId, setLabelId] = useState<string | undefined>(undefined);
  const close = (value?: unknown) => closeLayer(layer.id, value);

  useEffect(() => {
    // The dialog is named by its first heading (given an id here, since the content is rendered by callers).
    const heading = boxRef.current?.querySelector("h2");
    if (heading) { if (!heading.id) heading.id = titleId; setLabelId(heading.id); }
    const frame = requestAnimationFrame(() => {
      const box = boxRef.current;
      if (!box) return;
      const want = box.querySelector<HTMLElement>("[data-autofocus]") || box.querySelector<HTMLElement>("input, button.primary, button.accent, button.danger");
      want?.focus();
      // Nothing took focus (Settings' "Save changes" starts disabled): the close button, else the box itself.
      if (!box.contains(document.activeElement)) (box.querySelector<HTMLElement>("button[aria-label^='Close']") || box).focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [titleId]);

  useEffect(() => {
    if (!top) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); closeLayer(layer.id); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [top, layer.id]);

  return (
    <div className={`overlay ${layer.kind}-overlay${layer.leaving ? " leaving" : ""}`} role="dialog" aria-modal="true"
      aria-labelledby={labelId}
      onMouseDown={(e) => { if (e.target === e.currentTarget) close(undefined); }}>
      <div className={layer.kind} ref={boxRef} tabIndex={-1}>{layer.render(close)}</div>
    </div>
  );
}

function LayerHost() {
  const layers = useStore(layerStore, (s) => s.layers);
  const live = layers.filter((l) => !l.leaving);
  const topId = live.length ? live[live.length - 1].id : -1;
  return <>{layers.map((l) => <LayerView key={l.id} layer={l} top={l.id === topId} />)}</>;
}

// ---------------------------------------------------------------- menus

export interface MenuItem { label: string; icon?: string; danger?: boolean; action: () => void }
interface MenuState { anchor: Element; rect: DOMRect; items: MenuItem[]; key: number }
const menuStore = createStore({ menu: null as MenuState | null });
let menuSeq = 0;

export function openMenu(anchor: Element, items: MenuItem[]) {
  menuStore.set({ menu: { anchor, rect: anchor.getBoundingClientRect(), items, key: ++menuSeq } });
}

// Closing from the keyboard gives focus back to what opened the menu.
function closeMenu(refocus: boolean) {
  const anchor = menuStore.get().menu?.anchor as HTMLElement | undefined;
  menuStore.set({ menu: null });
  if (refocus && anchor?.isConnected) anchor.focus?.();
}

function MenuHost() {
  const menu = useStore(menuStore, (s) => s.menu);
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!menu || !el) { setPos(null); return; }
    const r = menu.rect;
    const w = el.offsetWidth;
    setPos({
      top: Math.min(r.bottom + 6, window.innerHeight - el.offsetHeight - 8),
      left: Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)),
    });
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const away = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) menuStore.set({ menu: null }); };
    const timer = setTimeout(() => document.addEventListener("mousedown", away));
    return () => { clearTimeout(timer); document.removeEventListener("mousedown", away); };
  }, [menu]);

  // Keyboard: focus starts on the first item, arrows move between items, Esc closes the menu (and only the
  // menu: captured here so the page's own Esc, which ends the conversation, never sees it).
  useEffect(() => {
    if (!menu) return;
    const items = () => [...(ref.current?.querySelectorAll<HTMLElement>("[role=menuitem]") || [])];
    const frame = requestAnimationFrame(() => items()[0]?.focus({ preventScroll: true }));
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); return; }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
      const list = items();
      if (!list.length) return;
      e.preventDefault();
      e.stopPropagation();
      const at = list.indexOf(document.activeElement as HTMLElement);
      const next = e.key === "Home" ? 0 : e.key === "End" ? list.length - 1
        : e.key === "ArrowDown" ? (at + 1) % list.length : (at <= 0 ? list.length - 1 : at - 1);
      list[next].focus();
    };
    document.addEventListener("keydown", onKey, true);
    return () => { cancelAnimationFrame(frame); document.removeEventListener("keydown", onKey, true); };
  }, [menu]);

  if (!menu) return null;
  return (
    <div className="menu" role="menu" ref={ref} key={menu.key}
      style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999 }}>
      {menu.items.map((item) => (
        <button key={item.label} className={item.danger ? "danger" : ""} role="menuitem"
          onClick={(e) => { e.stopPropagation(); closeMenu(e.detail === 0); item.action(); }}>
          {item.icon ? <Icon name={item.icon} className="sm" /> : null}
          {item.label}
        </button>
      ))}
    </div>
  );
}

export function OverlayHosts() {
  return (
    <>
      <Toaster />
      <LayerHost />
      <MenuHost />
    </>
  );
}
