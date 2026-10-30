"""What an endpoint is, and the ones Cometa has (one file each: endpoint_<name>.py).

An endpoint is a way to send notifications to Cometa: a path to POST to, how to turn its payload
into a notification, and what to do when it starts and stops. Subclass Endpoint, put it in
endpoint_<name>.py, and add it to build(). Its on/off switch is the setting endpoint_<name>
(module.json).
"""
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import service
from .achievements import https_url

ASSETS = Path(__file__).resolve().parent / "assets"
MAX_UNLOCK_AGE = 300  # seconds, /cometa/db only: an older unlock is Comet syncing what you had, not something new


@lru_cache(maxsize=None)
def default_icon():
    """Cometa's own image (assets/comety.png) as a data: URI, or "" if it can't be read."""
    try:
        return service.icon_from_file(ASSETS / "comety.png")
    except service.Rejected:
        return ""


def unlock_age(unlock_time):
    """How long ago (seconds) Comet's unlock_time ("2026-07-31T01:48:51+0000") was, or None if it isn't one.
    /cometa/db checks it, as Comety sends every update of an achievement, including a new database being filled
    with what you already had. /cometa doesn't: its time is the game's, which stands still while the machine
    sleeps, and Comet sends that event only for a new unlock."""
    try:
        when = datetime.strptime(unlock_time, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


def logo(files):
    """The first of the game's logo files (Host.logos) that fits, as a data: URI, or "" if none does."""
    for path in files:
        try:
            return service.icon_from_file(path)
        except service.Rejected:
            continue  # too big, or not an image: the next one
    return ""


def trophy(name, description, rarity, image_url):
    """An unlocked achievement as (title, body, icon, sound): the description and the rarity as the body, the
    https:// image as the icon, always the trophy sound."""
    body = description if isinstance(description, str) else ""
    if isinstance(rarity, (int, float)) and not isinstance(rarity, bool):
        body = f"{body} · {rarity:.1f}%" if body else f"{rarity:.1f}%"
    return name, body, https_url(image_url), "trophy"


class Endpoint:
    name = ""     # the setting is endpoint_<name>; also the "source" of its notifications
    path = ""     # POST here
    label = ""    # how its messages call it
    icon = None   # file in assets/ with its image: used by its own messages

    def adapt(self, data):
        """The payload (a dict) as (title, body, icon[, sound]), or None to ignore it. Rejected(400, …)
        if it's wrong."""
        raise NotImplementedError

    def start(self, host):
        """The endpoint is on: the service is running and its switch is on."""

    def stop(self):
        """It's off, or the service is stopping."""

    def connected(self, host):
        """The user turned it on. Say so (an endpoint may say it in its own way, or not at all)."""
        host.announce(self, f"{self.label} connected", f"Cometa shows what is POSTed to {self.path}.", sound="online")

    def disconnected(self, host):
        """The user is turning it off (called before stop())."""
        host.announce(self, f"{self.label} disconnected", "Cometa no longer shows its notifications.", sound="mention")

    def details(self):
        """Lines for the tab, under the endpoint, while it's on."""
        return []

    # -- Invasor's game events, for the endpoints that follow the games --

    @property
    def listening(self):
        return bool(getattr(self, "_handlers", None))

    def _listen(self, host, handlers):
        """Follow Invasor's events, {name: callback}, until _unlisten()."""
        self._handlers = handlers
        for name, cb in handlers.items():
            host.ctx.events.on(name, cb)

    def _unlisten(self):
        handlers, self._handlers = getattr(self, "_handlers", None) or {}, {}
        for name, cb in handlers.items():
            try:
                self.host.ctx.events.off(name, cb)
            except Exception:
                self.host.log.exception("can't stop listening to %s", name)

    def icon_data(self):
        """Its image as a data: URI, or "" if it has none."""
        if not hasattr(self, "_icon_data"):
            try:
                self._icon_data = service.icon_from_file(ASSETS / self.icon) if self.icon else ""
            except service.Rejected:
                self._icon_data = ""
        return self._icon_data

    def icon_or_default(self):
        """Its image, or Cometa's own if it has none."""
        return self.icon_data() or default_icon()


def build():
    """Every endpoint, by name, in the order they are listed."""
    from . import endpoint_comet, endpoint_comet_db
    endpoints = [endpoint_comet.Comet(), endpoint_comet_db.CometDb()]
    return {ep.name: ep for ep in endpoints}
