"""Cometa: show Comet's (GOG) achievements as Steam notifications, from a local web service.

Comet POSTs JSON to http://127.0.0.1:<port>/cometa, and Comety to /cometa/db (see
endpoints.py), and Invasor's core shows it
(ctx.notify). If Steam can't show notifications, the request gets a 503 and nothing else
is affected; if the port is taken, the tab says so. Each endpoint can be turned off, and
"do not disturb" silences them all; both are read live, without restarting the service.
Each endpoint is told when it starts and stops (endpoints.Endpoint), and can announce itself.
"""
import threading

from . import achievements as achievements_mod
from . import endpoints as endpoints_mod
from . import games, service

ctx = None
server = None
error = None
eps = {}  # name -> Endpoint
host = None
_lock = threading.Lock()


class Host:
    """What an endpoint can use of Cometa."""

    def __init__(self, context):
        self.ctx, self.log = context, context.log

    def prefix(self, appid):
        """The game's compatdata folder, in whichever library holds it (ctx.paths)."""
        return self.ctx.paths.compatdata(appid)

    def logos(self, appid):
        """The game's logo images, best first; [] if Steam's folders can't be found."""
        try:
            return games.logo_files(appid, self.ctx.paths.steam_root(), self.ctx.paths.userdata(all=True))
        except (self.ctx.InvalidArgument, self.ctx.Unavailable):
            return []

    @property
    def port(self):
        return server.port if server else None

    def announce(self, endpoint, title, body="", icon="", sound=""):
        """A message of the endpoint itself, with its image unless there's a better icon (or
        Cometa's default one, if the endpoint has none). It follows "do not disturb" and the
        rate limit; it doesn't need the endpoint to be on."""
        s = server
        if not s:
            return
        try:
            s.receive(endpoint.name, title[:64], body[:256], icon or endpoint.icon_or_default(), announce=True, sound=sound)
        except service.Rejected as e:
            self.log.warning("%s: message not shown: %s", endpoint.name, e)


def _on(endpoint):
    return bool(ctx.settings.get(f"endpoint_{endpoint.name}"))


def _icon_for(name):
    """The icon of a notification of the endpoint that has none: its image, or Cometa's."""
    return eps[name].icon_or_default() if name in eps else endpoints_mod.default_icon()


def _stop_endpoints():
    for ep in eps.values():
        try:
            ep.stop()
        except Exception:
            ctx.log.exception("%s: stopping failed", ep.name)


def _stop_service():
    """Stop the endpoints and the web service (with _lock held)."""
    global server
    _stop_endpoints()
    if server:
        server.stop()
        server = None


def _start():
    """(Re)start the service with the current settings. Never raises."""
    global server, error
    with _lock:
        _stop_service()
        error = None
        if not ctx.settings.get("enabled"):
            return
        port = ctx.settings.get("port")
        s = service.CometaServer(port, ctx.notify, ctx.log, endpoints=eps.values(), unavailable=(ctx.Unavailable,),
                               endpoint_on=lambda name: ctx.settings.get(f"endpoint_{name}"),
                               quiet=lambda: ctx.settings.get("dnd"), default_icon=_icon_for)
        try:
            s.start()
        except OSError as e:
            error = f"can't listen on 127.0.0.1:{port} ({e.strerror or e})"
            ctx.log.warning("%s", error)
            return
        server = s
        ctx.log.info("listening on http://127.0.0.1:%s", port)
        for ep in eps.values():
            if _on(ep):
                _safely(ep.start, host)


def _safely(call, *args):
    try:
        call(*args)
    except Exception:
        ctx.log.exception("endpoint hook failed")


def _toggle(key, value):
    """The user turned an endpoint on or off."""
    ep = eps.get(key[len("endpoint_"):])
    with _lock:
        if not ep or not server:
            return
        if value:
            _safely(ep.start, host)
            _safely(ep.connected, host)
        else:
            _safely(ep.disconnected, host)
            _safely(ep.stop)


def _changed(key, value):
    # Only these need a new server: endpoints and "do not disturb" are read live.
    if key in ("enabled", "port"):
        _start()
    elif key.startswith("endpoint_"):
        _toggle(key, value)


def setup(context):
    global ctx, host, eps
    ctx = context
    host = Host(context)
    eps = endpoints_mod.build()
    context.settings.on_change(_changed)
    _start()


def teardown():
    with _lock:
        _stop_service()


def status():
    port = ctx.settings.get("port")
    base = f"http://127.0.0.1:{port}"
    running = server is not None
    listed = [{"name": ep.name, "path": ep.path, "url": base + ep.path, "on": _on(ep),
               "details": ep.details() if running and _on(ep) else []} for ep in eps.values()]
    return {"enabled": ctx.settings.get("enabled"), "running": running, "port": port,
            "url": base, "error": error, "dnd": ctx.settings.get("dnd"), "endpoints": listed}


def recent():
    return list(server.recent) if server else []


def test():
    """Show a test notification (through the service's rate limit, as a POST would). It
    ignores "do not disturb" and the endpoints: the user asked for it."""
    s = server or service.CometaServer(0, ctx.notify, ctx.log, unavailable=(ctx.Unavailable,))
    try:
        s.notify("Cometa", "This is a test notification from Invasor.", endpoints_mod.default_icon())
    except service.Rejected as e:
        raise (ctx.Unavailable if e.status == 503 else ctx.InvalidArgument)(str(e)) from None
    return True


def is_gog(appid):
    """Whether the game is a GOG one, so it runs Comet: a shortcut whose .bat says set "STORE=gog"."""
    return games.is_gog_launcher(ctx.game.shortcut_exe(appid))


def achievements(appid):
    """The achievements Comet keeps for the game, one entry per gameplay.db: {"gog": False} if it isn't a GOG
    game."""
    if not is_gog(appid):
        return {"gog": False}
    return {"gog": True, "dbs": [achievements_mod.read(db) for db in games.gameplay_dbs(ctx.paths.compatdata(appid))]}


METHODS = {"status": status, "recent": recent, "test": test, "achievements": achievements, "is_gog": is_gog}
