/**
 * One WebSocket to the backend for the whole app.
 *
 * Components declare what they want with `want(key)` and release it when they unmount.
 * Keys look like "kline:BTCUSDT:1m", "ticker:ETHUSDT", "depth:SOLUSDT". The client
 * ref-counts them and sends the union to the server, which fans out only those topics.
 */
import { useEffect, useState } from "react";
import type { ServerMsg, Status } from "./types";

type Listener = (m: ServerMsg) => void;

class WsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private wanted = new Map<string, number>();
  private attempt = 0;
  private flushScheduled = false;
  socketOpen = false;
  lastStatus: Status | null = null;
  lastMessageAt = 0;

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.socketOpen = true;
      this.attempt = 0;
      this.sendSubscription();
      this.emitLocal();
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data) as ServerMsg;
      this.lastMessageAt = Date.now();
      if (msg.type === "status") this.lastStatus = msg;
      for (const l of this.listeners) l(msg);
    };
    ws.onclose = (ev) => {
      this.socketOpen = false;
      this.emitLocal();
      if (ev.code === 4401) {                       // no session: the login screen reconnects us
        window.dispatchEvent(new Event("wick:unauthorized"));
        return;
      }
      // Same shape of backoff as the backend uses toward Binance.
      const delay = Math.min(30_000, 1000 * 2 ** this.attempt) * (0.5 + Math.random());
      this.attempt += 1;
      setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  /** Re-broadcast the last status so hooks re-render on local socket changes. */
  private emitLocal() {
    if (this.lastStatus) for (const l of this.listeners) l(this.lastStatus);
  }

  subscribe(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  want(key: string): () => void {
    this.wanted.set(key, (this.wanted.get(key) ?? 0) + 1);
    this.scheduleFlush();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const n = (this.wanted.get(key) ?? 1) - 1;
      if (n <= 0) this.wanted.delete(key);
      else this.wanted.set(key, n);
      this.scheduleFlush();
    };
  }

  private scheduleFlush() {
    if (this.flushScheduled) return;
    this.flushScheduled = true;
    queueMicrotask(() => {
      this.flushScheduled = false;
      this.sendSubscription();
    });
  }

  private sendSubscription() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const klines: [string, string][] = [];
    const tickers: string[] = [];
    let depth: string | null = null;
    for (const key of this.wanted.keys()) {
      const [kind, symbol, interval] = key.split(":");
      if (kind === "kline") klines.push([symbol, interval]);
      else if (kind === "ticker") tickers.push(symbol);
      else if (kind === "depth") depth = symbol; // one context panel at a time
    }
    this.ws.send(JSON.stringify({ op: "subscribe", klines, tickers, depth }));
  }
}

export const wsClient = new WsClient();
wsClient.connect();

/** Subscribe a callback to every server message for the lifetime of the component. */
export function useServerMessages(handler: Listener, deps: unknown[]) {
  useEffect(() => wsClient.subscribe(handler), deps); // eslint-disable-line react-hooks/exhaustive-deps
}

export type ConnState = "live" | "reconnecting" | "stale";

/**
 * live:         browser socket open, backend socket to Binance open, frames flowing
 * reconnecting: either socket is down
 * stale:        sockets are up but nothing has arrived recently (frozen data)
 */
export function useConnectionStatus(): { state: ConnState; status: Status | null } {
  const [, tick] = useState(0);
  useEffect(() => {
    const unsub = wsClient.subscribe(() => tick((n) => n + 1));
    const timer = setInterval(() => tick((n) => n + 1), 2000);
    return () => { unsub(); clearInterval(timer); };
  }, []);
  const s = wsClient.lastStatus;
  let state: ConnState = "reconnecting";
  if (wsClient.socketOpen && s && s.ingest.state === "live") {
    const backendAge = s.ingest.lastFrameAgeS ?? Infinity;
    const localAge = (Date.now() - wsClient.lastMessageAt) / 1000;
    state = backendAge > 10 || localAge > 8 ? "stale" : "live";
  }
  return { state, status: s };
}
