import { defineModule, ui, type ModuleCtx } from "invasor";

// Cometa: a local web service that shows Steam notifications (as Steam shows an
// achievement). General shows whether it's listening, its settings, a test button and
// the last notifications (shown, failed or silenced); then Achievements and Credits.

interface Endpoint {
  name: string;
  path: string;
  url: string;
  on: boolean;
  details: string[];
}

interface Status {
  enabled: boolean;
  running: boolean;
  port: number;
  url: string;
  error: string | null;
  dnd: boolean;
  endpoints: Endpoint[];
}

interface Recent {
  time: string;
  source: string;
  title: string;
  ok: boolean;
  error: string | null;
  muted: "endpoint_off" | "do_not_disturb" | null;
}

const MUTED = { endpoint_off: "endpoint off", do_not_disturb: "do not disturb" };

function recentLine(r: Recent) {
  const mark = r.muted ? "🔕" : r.ok ? "✓" : "✗";
  const why = r.muted ? ` — ${MUTED[r.muted]}` : r.error ? ` — ${r.error}` : "";
  return `${r.time}  ${mark} [${r.source}] ${r.title}${why}`;
}

const errorText = (e: unknown) => String((e as Error)?.message ?? e);

let statusEl: HTMLElement | null = null;
let recentEl: HTMLElement | null = null;
let timer: number | undefined;

async function refresh(ctx: ModuleCtx) {
  if (!statusEl || !recentEl) return;
  try {
    const st = await ctx.call<Status>("status");
    const line = !st.enabled
      ? "The web service is off."
      : st.running
        ? `Listening on ${st.url}`
        : `⚠ Not listening: ${st.error ?? "unknown error"}`;
    statusEl.replaceChildren(
      ui.info(line),
      ...(st.running && st.dnd ? [ui.info("🔕 Do not disturb: requests are answered, but nothing is shown.")] : []),
    );
    const list = await ctx.call<Recent[]>("recent");
    recentEl.replaceChildren(
      ...(list.length
        ? list.map((r) => ui.info(recentLine(r)))
        : [ui.info("Nothing sent yet.")]),
    );
  } catch (e) {
    statusEl.replaceChildren(ui.info(errorText(e)));
  }
}

async function renderGeneral(el: HTMLElement, ctx: ModuleCtx) {
  statusEl = document.createElement("div");
  recentEl = document.createElement("div");
  const service = await ui.settingsForm(ctx, { keys: ["enabled", "port", "dnd"], navHints: false });
  const endpoints = await ui.settingsForm(ctx, { keys: ["endpoint_comet", "endpoint_comet_db"], navHints: false });
  el.append(
    statusEl,
    service,
    ui.separator(),
    endpoints,
    ui.button({
      label: "Send a test notification",
      onClick: async () => {
        try {
          await ctx.call("test");
        } catch (e) {
          ctx.toast(`Not shown: ${errorText(e)}`, "error");
        }
        void refresh(ctx);
      },
    }),
    ui.section("Last notifications", [recentEl], { open: true }),
  );
  await refresh(ctx);
  // Saving a setting restarts the service, and notifications arrive at any time: keep
  // the state fresh while the tab is on screen.
  timer = window.setInterval(() => {
    if (el.isConnected && el.getClientRects().length) void refresh(ctx);
  }, 3000);
}

export default defineModule({
  tabs: [
    {
      label: "General",
      render: (el, ctx) => renderGeneral(el, ctx),
      onShow: (ctx) => void refresh(ctx),
    },
    {
      label: "Achievements",
      render(el) {
        el.append(ui.info("Coming soon."));
      },
    },
    {
      label: "Credits",
      render(el) {
        el.append(
          ui.info("Cometa shows the achievements of GOG games that Comet (github.com/imLinguin/comet) reports, as Steam notifications."),
          ui.separator(),
          ui.info("Thanks to imLinguin and every Comet contributor. Comet is their own project, under the Apache License 2.0."),
          ui.separator(),
          ui.info("Cometa is not affiliated with Comet or imLinguin. Please don't report problems with this module to them."),
        );
      },
    },
  ],
  tabsAlign: "justify",
  destroy: () => {
    window.clearInterval(timer);
    statusEl = recentEl = null;
  },
});
