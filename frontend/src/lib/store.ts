// A minimal external store: plain objects updated by code anywhere (event handlers, the voice client,
// timers), read by components through useStore with a selector.
import { useSyncExternalStore } from "react";

export interface Store<T> {
  get(): T;
  set(patch: Partial<T> | ((s: T) => Partial<T>)): void;
  subscribe(fn: () => void): () => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      listeners.forEach((fn) => fn());
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export function useStore<T extends object, S>(store: Store<T>, select: (s: T) => S, serverValue?: S): S {
  return useSyncExternalStore(
    store.subscribe,
    () => select(store.get()),
    () => (serverValue !== undefined ? serverValue : select(store.get())),
  );
}
