import { currentGame, defineModule, ui, type ModuleCtx } from "invasor";

// Cometa: a local web service that shows Steam notifications (as Steam shows an
// achievement). General shows whether it's listening, its settings, a test button and
// the last notifications (shown, failed or silenced); Achievements, the achievements Comet keeps
// for the current GOG game; then Credits.

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

interface Achievement {
  key: string;
  name: string;
  description: string;
  hidden: boolean;
  unlocked: string | null;
  rarity: number;
  rarity_level: string;
  image: string;
}

interface Database {
  client_id: string;
  error?: string;
  unlocked?: number;
  total?: number;
  achievements?: Achievement[];
}

interface Achievements {
  gog: boolean;
  dbs?: Database[];
}

let achievementsEl: HTMLElement | null = null;
let shownAppid: string | null = null;
let showHidden = false;
type Filter = "all" | "unlocked" | "locked";
let filter: Filter = "all";
let last: { name: string; result: Achievements } | null = null;

// Comet's times are like 2026-10-11T00:50:24+0000: shown in local time.
function when(iso: string) {
  const d = new Date(iso.replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const secret = (a: Achievement) => a.hidden && !a.unlocked && !showHidden;

function rarity(a: Achievement) {
  return `${a.rarity.toFixed(1)}%${a.rarity_level ? ` (${a.rarity_level})` : ""}`;
}

// One row per achievement: a 32×32 icon (the same size whatever GOG serves), its name and, under it,
// its description, rarity and when it was unlocked. A navigation stop, so ↑↓ scroll the list.
function achievementRow(a: Achievement) {
  const hide = secret(a);
  const row = document.createElement("div");
  row.className = "ctl";
  row.dataset.nav = "";
  row.dataset.hint = hide ? "Hidden achievement" : a.description || a.name;
  row.style.cssText = "flex-wrap: nowrap; justify-content: flex-start; gap: 10px; cursor: default";
  const icon = document.createElement("img");
  icon.alt = "";
  icon.style.cssText =
    "flex: none; width: 32px; height: 32px; object-fit: cover; border-radius: 4px; background: rgba(255,255,255,0.08)";
  if (a.image) icon.src = a.image;
  if (!a.unlocked) icon.style.opacity = "0.6";
  icon.onerror = () => icon.removeAttribute("src");
  const text = document.createElement("div");
  text.style.cssText = "min-width: 0";
  const name = document.createElement("div");
  name.textContent = `${a.unlocked ? "✓" : "🔒"} ${hide ? "Hidden achievement" : a.name}`;
  if (hide) name.style.color = "#e5534b";
  const more = document.createElement("div");
  more.style.cssText = "color: #8b94a7; font-size: 13px";
  more.textContent = [hide ? "" : a.description, rarity(a), a.unlocked ? `Unlocked ${when(a.unlocked)}` : "Locked"]
    .filter(Boolean)
    .join(" · ");
  text.append(name, more);
  row.append(icon, text);
  return row;
}

const summary = (db: Database) => `${db.unlocked} of ${db.total} unlocked`;

// The database's achievements that pass the filter, as rows (or why there are none).
function rows(db: Database): HTMLElement[] {
  if (db.error || !db.achievements) return [ui.info(`Can't read it: ${db.error}`)];
  if (!db.achievements.length) return [ui.info("This game has no achievements.")];
  const shown = db.achievements.filter((a) => filter === "all" || (filter === "unlocked") === !!a.unlocked);
  return shown.length ? shown.map(achievementRow) : [ui.info(`No ${filter} achievements.`)];
}

function paintAchievements() {
  if (!achievementsEl || !last) return;
  const { name, result } = last;
  achievementsEl.replaceChildren(
    ...(!result.gog
      ? [ui.info(`${name} isn't a GOG game: Comet only runs for GOG games.`)]
      : !result.dbs?.length
        ? [ui.info(`${name}: no achievements yet. Comet keeps them once the game has run with it.`)]
        : result.dbs.length === 1 // one database: no need to group them
          ? [ui.info(result.dbs[0].error ? name : `${name} — ${summary(result.dbs[0])}`), ...rows(result.dbs[0])]
          : [ui.info(name), ...result.dbs.map((db) => ui.section(db.error ? db.client_id : `${db.client_id} — ${summary(db)}`, rows(db)))]),
  );
}

async function loadAchievements(ctx: ModuleCtx) {
  if (!achievementsEl) return;
  const current = currentGame(ctx.game());
  const appid = current?.game.appid ?? null;
  if (appid === shownAppid) return;
  shownAppid = appid;
  last = null;
  if (!current || !appid) {
    achievementsEl.replaceChildren(ui.info("Choose a GOG game in the library to see its achievements."));
    return;
  }
  const name = current.game.name ?? appid;
  achievementsEl.replaceChildren(ui.info(`Reading the achievements of ${name}…`));
  try {
    const result = await ctx.call<Achievements>("achievements", { appid });
    if (shownAppid !== appid) return; // another game meanwhile
    last = { name, result };
    paintAchievements();
  } catch (e) {
    if (shownAppid !== appid) return;
    shownAppid = null; // try again next time
    achievementsEl?.replaceChildren(ui.info(errorText(e)));
  }
}

async function renderAchievements(el: HTMLElement, ctx: ModuleCtx) {
  achievementsEl = document.createElement("div");
  el.append(
    ui.toggle({
      label: "Show hidden achievements",
      value: showHidden,
      hint: "Shows the name and description of the hidden achievements you don't have yet.",
      onChange: (v) => {
        showHidden = v;
        paintAchievements();
      },
    }),
    ui.radio<Filter>({
      label: "Filter",
      value: filter,
      options: [
        { value: "all", label: "All" },
        { value: "unlocked", label: "Unlocked" },
        { value: "locked", label: "Locked" },
      ],
      onChange: (v) => {
        filter = v;
        paintAchievements();
      },
    }),
    achievementsEl,
  );
  await loadAchievements(ctx);
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
      render: (el, ctx) => renderAchievements(el, ctx),
      onShow: (ctx) => void loadAchievements(ctx),
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
  onGameChange: (_game, ctx) => void loadAchievements(ctx),
  // Quick Access only while a GOG game is being played: that's when its achievements matter.
  showInQam: async (ctx) => {
    const running = ctx.game().running;
    return !!running?.shortcut && (await ctx.call<boolean>("is_gog", { appid: running.appid }));
  },
  destroy: () => {
    window.clearInterval(timer);
    statusEl = recentEl = achievementsEl = null;
    shownAppid = null;
    last = null;
  },
});
