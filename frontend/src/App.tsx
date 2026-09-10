import { useEffect, useState } from "react";
import { api, CLERK_KEY, setTokenProvider, UNAUTHORIZED } from "./api";
import { RedirectToSignIn, SignOutButton, UserButton, useAuth } from "@clerk/react";
import { useLocalStorage } from "./store";
import { useConnectionStatus, wsClient } from "./ws";
import StatusBadge from "./components/StatusBadge";
import Analysis from "./screens/Analysis";
import Prop from "./screens/Prop";
import Charts from "./screens/Charts";
import Context from "./screens/Context";
import Market from "./screens/Market";

type Screen = "charts" | "market" | "context" | "analysis" | "prop";

/** Tiny hash router: #/charts?symbol=BTCUSDT. No dependency needed for three screens. */
function parseHash(): { screen: Screen; params: URLSearchParams } {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const screen = (["charts", "market", "context", "analysis", "prop"].includes(path) ? path : "charts") as Screen;
  return { screen, params: new URLSearchParams(query) };
}

export function navigate(screen: Screen, params?: Record<string, string>) {
  const q = params ? "?" + new URLSearchParams(params).toString() : "";
  location.hash = `#/${screen}${q}`;
}

/** Shared-password login. Shown when the backend says a session is required and we have none. */
function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.login(password);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally { setBusy(false); }
  };
  return (
    <div className="fixed inset-0 z-50 bg-zinc-950 flex items-center justify-center">
      <form onSubmit={submit} className="w-80 space-y-4">
        <div className="font-semibold text-lg">Wick</div>
        <input autoFocus type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password"
               className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 outline-none focus:border-zinc-500" />
        {error && <div className="text-sm text-red-400">{error}</div>}
        <button disabled={busy || !password} className="w-full bg-zinc-100 text-zinc-900 rounded py-2 font-medium disabled:opacity-50">
          {busy ? "Signing in…" : "Sign In"}
        </button>
      </form>
    </div>
  );
}

/** Shown after a start until stored history is continuous again. Pages work; research and trade actions wait. */
function SyncBanner() {
  const { status } = useConnectionStatus();
  if (!status || status.ready !== false) return null;
  const p = status.ingest.syncProgress;
  const pct = p && p.total ? Math.round((100 * p.done) / p.total) : null;
  return (
    <div className="px-4 py-1.5 text-sm bg-amber-900/40 text-amber-200 border-b border-amber-900 shrink-0">
      Syncing market history{pct != null ? ` (${pct}%)` : ""}. You can look around; research and trade actions unlock when it finishes.
    </div>
  );
}

/** Clerk mode wrapper: hands the session token to api/ws, redirects signed-out visitors to the
 *  hosted sign-in page (accounts.candlelit.us in production), and holds the beta gate. */
function ClerkGate({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const [access, setAccess] = useState<{ status: string | null; message?: string } | null>(null);
  useEffect(() => {
    if (!isSignedIn) return;
    const provider = () => getToken();
    setTokenProvider(provider);
    wsClient.tokenProvider = provider;
    wsClient.connect();
    api.auth().then((a) => setAccess({ status: a.accessStatus, message: a.betaMessage })).catch(() => setAccess({ status: null }));
  }, [isSignedIn, getToken]);
  if (!isLoaded) return <div className="h-full flex items-center justify-center text-zinc-500">Loading…</div>;
  if (!isSignedIn) return <RedirectToSignIn redirectUrl={location.href} />;
  if (access && access.status !== "active") {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="max-w-md text-center space-y-4">
          <div className="text-xs uppercase tracking-[0.3em] text-zinc-500">Wick</div>
          <div className="text-2xl font-semibold">Private beta</div>
          <p className="text-zinc-400">{access.message ?? "Wick is currently in private beta. Access for this account has not been enabled yet."}</p>
          <p className="text-sm text-zinc-500">Write to <a className="underline" href="mailto:tradingcandlelight@gmail.com">tradingcandlelight@gmail.com</a> to request access.</p>
          <SignOutButton><button className="text-sm text-zinc-400 hover:text-zinc-200 border border-zinc-800 rounded px-3 py-1">Sign out</button></SignOutButton>
        </div>
      </div>
    );
  }
  if (!access) return <div className="h-full flex items-center justify-center text-zinc-500">Loading…</div>;
  return <>{children}</>;
}

export default function App() {
  if (CLERK_KEY) return <ClerkGate><Shell /></ClerkGate>;
  return <Shell />;
}

function Shell() {
  const [route, setRoute] = useState(parseHash);
  const [needLogin, setNeedLogin] = useState(false);
  useEffect(() => {
    if (CLERK_KEY) return;                        // Clerk mode: the gate above owns sign-in
    api.auth().then((a) => setNeedLogin(a.enabled && !a.loggedIn)).catch(() => {});
    const onUnauthorized = () => setNeedLogin(true);
    window.addEventListener(UNAUTHORIZED, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED, onUnauthorized);
  }, []);
  const loggedIn = () => { setNeedLogin(false); wsClient.connect(); location.reload(); };
  const [density, setDensity] = useLocalStorage<"comfortable" | "compact">("density", "comfortable");
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  // The root font size is the single knob for the whole app's typography (see index.css).
  useEffect(() => { document.documentElement.dataset.density = density; }, [density]);

  const tab = (s: Screen, label: string) => (
    <button
      onClick={() => navigate(s)}
      className={`px-3 py-1.5 rounded text-[15px] ${route.screen === s ? "bg-zinc-800 text-white" : "text-zinc-400 hover:text-zinc-200"}`}
    >
      {label}
    </button>
  );

  if (needLogin) return <Login onDone={loggedIn} />;
  return (
    <div className="h-full flex flex-col">
      <SyncBanner />
      <header className="flex items-center gap-2 px-4 py-2 border-b border-zinc-800 shrink-0">
        <span className="font-semibold tracking-tight mr-4">Wick</span>
        {tab("charts", "Charts")}
        {tab("market", "Market")}
        {tab("context", "Context")}
        {tab("analysis", "Analysis")}
        {tab("prop", "Trade")}
        <div className="ml-auto flex items-center gap-3">
          <StatusBadge />
          <button onClick={() => setDensity((d) => (d === "compact" ? "comfortable" : "compact"))} className="text-xs text-zinc-500 hover:text-zinc-300 border border-zinc-800 rounded px-2 py-0.5" title="Text density">
            {density === "compact" ? "Compact" : "Comfortable"}
          </button>
          {CLERK_KEY && <UserButton />}
        </div>
      </header>
      <main className="flex-1 min-h-0 overflow-auto">
        {/* A fresh keyed wrapper per screen replays a 160 ms fade, so switching feels settled, not swapped. */}
        <div key={route.screen} className="screen-enter h-full">
          {route.screen === "charts" && <Charts params={route.params} />}
          {route.screen === "market" && <Market />}
          {route.screen === "context" && <Context params={route.params} />}
          {route.screen === "analysis" && <Analysis params={route.params} />}
          {route.screen === "prop" && <Prop />}
        </div>
      </main>
    </div>
  );
}
