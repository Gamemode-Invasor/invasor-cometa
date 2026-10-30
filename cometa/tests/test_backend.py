import http.client
import json
import logging
import os
import socket
import tempfile
import time
import types
import unittest
from unittest import mock

from tests.loader import HERE, free_port, load_backend, submodule
MODULE = json.loads((HERE / "module.json").read_text())


def defaults():
    """Every setting's default, as the core would give them (sections flattened)."""
    fields = []
    for item in MODULE["settings"]:
        fields += item.get("items", [item])
    return {f["key"]: f["default"] for f in fields}


class InvalidArgument(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class FakeSettings:
    def __init__(self, values):
        self.values, self.cbs = values, []

    def get(self, key=None):
        return self.values if key is None else self.values[key]

    def on_change(self, cb):
        self.cbs.append(cb)

    def change(self, key, value):
        self.values[key] = value
        for cb in self.cbs:
            cb(key, value)


class FakeEvents:
    def __init__(self):
        self.cbs = {}

    def on(self, name, cb):
        self.cbs.setdefault(name, []).append(cb)

    def off(self, name, cb):
        self.cbs[name].remove(cb)

    def emit(self, name, payload):
        for cb in list(self.cbs.get(name, [])):
            cb(dict(payload))


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self, port):
        self.settings = FakeSettings({**defaults(), "enabled": True, "port": port})
        self.log = logging.getLogger("test.cometa")
        self.shown, self.bodies, self.icons, self.sounds = [], [], [], []
        self.game = types.SimpleNamespace(running=None, selected=None, shortcut_exe=lambda appid: None)
        self.events = FakeEvents()
        self.paths = types.SimpleNamespace(  # Steam's folders as ctx.paths gives them (a game's prefix in a 2nd library)
            compatdata=lambda appid: f"/sd/steamapps/compatdata/{appid}", steam_root=lambda: "/steam",
            userdata=lambda all=False: ["/steam/userdata/1"])

    def stop_game(self):
        """The game being played ends: running goes, and the core sends game_stopped."""
        game, self.game.running = self.game.running, None
        self.events.emit("game_stopped", game)

    def notify(self, title, body="", icon="", sound=""):
        self.shown.append(title)
        self.bodies.append(body)
        self.icons.append(icon)
        self.sounds.append(sound)


class Backend(unittest.TestCase):
    def test_host_finds_steam_s_folders_with_ctx_paths(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        host = b.Host(ctx)
        self.assertEqual(host.prefix("123"), "/sd/steamapps/compatdata/123")
        with mock.patch.object(submodule(b, "games"), "logo_files", return_value=["/x.png"]) as files:
            self.assertEqual(host.logos("123"), ["/x.png"])
            files.assert_called_with("123", "/steam", ["/steam/userdata/1"])

        def no_steam():
            raise Unavailable("Steam's folder wasn't found")
        ctx.paths.steam_root = no_steam
        self.assertEqual(host.logos("123"), [])  # no logo, no error

    def test_lifecycle(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.assertTrue(b.status()["running"])
        self.assertTrue(b.test())
        self.assertEqual(ctx.shown, ["Cometa"])
        self.assertEqual(b.recent()[0]["title"], "Cometa")
        ctx.settings.change("enabled", False)
        self.assertEqual((b.status()["running"], b.status()["error"]), (False, None))
        self.assertTrue(b.test())  # the test button works with the service off too

    def test_silences_are_read_live(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        server = b.server
        ctx.settings.change("dnd", True)
        ctx.settings.change("endpoint_comet_db", False)
        self.assertIs(b.server, server)  # no restart
        st = b.status()
        self.assertTrue(st["dnd"])
        self.assertEqual([(e["path"], e["on"]) for e in st["endpoints"]], [("/cometa", True), ("/cometa/db", False)])
        self.assertEqual(st["endpoints"][1]["url"], f"http://127.0.0.1:{st['port']}/cometa/db")
        self.assertEqual(server.receive("comet", "x", "", ""), "do_not_disturb")
        self.assertEqual(server.receive("comet_db", "x", "", ""), "endpoint_off")
        self.assertTrue(b.test())  # the test button ignores the silences
        self.assertEqual(ctx.shown, ["Cometa"])
        ctx.settings.change("dnd", False)
        self.assertIsNone(server.receive("comet", "x", "", ""))
        self.assertEqual(ctx.shown, ["Cometa", "x"])
        ctx.settings.change("port", free_port())
        self.assertIsNot(b.server, server)  # the port does restart it

    def started(self, running, dbs=("/a/gameplay.db", "/b/gameplay.db")):
        """A backend with a game running, a (mocked) comety, and the databases that were found."""
        b = load_backend()
        comet = submodule(b, "endpoint_comet_db")
        comet.GAME_WAITS = (0.01, 0.01, 0.01)
        comet.games = mock.Mock(gameplay_dbs=mock.Mock(return_value=list(dbs)), logo_files=mock.Mock(return_value=[]))
        popen = mock.patch.object(comet.subprocess, "Popen").start()
        popen.return_value.poll.return_value = None
        chmod = mock.patch.object(comet.os, "chmod").start()
        self.addCleanup(mock.patch.stopall)
        ctx = FakeCtx(free_port())
        ctx.settings.values["endpoint_comet_db"] = True  # off by default: the old method
        ctx.game.running = running
        return b, comet, ctx, popen, chmod

    def wait_for(self, cond):
        for _ in range(300):
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def details(self, b):
        return b.status()["endpoints"][1]["details"]

    def test_comety_runs_for_each_database_of_the_game_being_played(self):
        b, comet, ctx, popen, chmod = self.started({"appid": "123", "name": "Lil Gator Game"})
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.call_count == 2)
        url = f"http://127.0.0.1:{b.status()['port']}/cometa/db"
        self.assertEqual([c.args[0][1:] for c in popen.call_args_list],
                         [["--url", url, "--quiet", "/a/gameplay.db"], ["--url", url, "--quiet", "/b/gameplay.db"]])
        chmod.assert_called_with(comet.COMETY_BIN, 0o755)
        comet.games.gameplay_dbs.assert_called_with("/sd/steamapps/compatdata/123")  # the library that holds the game
        self.assertEqual(self.details(b), ["● Comety: /a/gameplay.db", "● Comety: /b/gameplay.db"])
        self.assertEqual((ctx.shown, ctx.bodies), (["Comet - Lil Gator Game"], ["Cometa is connecting to Lil Gator Game"]))
        self.assertEqual(ctx.sounds, ["online"])
        ctx.stop_game()  # the game ends: comety stops, and it's said
        self.wait_for(lambda: self.details(b) == ["Comety: waiting for a game with Comet"])
        self.assertEqual(popen.return_value.terminate.call_count, 2)
        self.assertEqual((ctx.shown[1], ctx.bodies[1]), ("Comet - Lil Gator Game", "Cometa disconnected from Lil Gator Game"))
        self.assertEqual(ctx.sounds[1], "mention")

    def test_the_game_message_has_its_logo_or_comets_image(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: ctx.icons)
        self.assertTrue(ctx.icons[0].startswith("data:image/png;base64,"))  # no logo: comety.png
        self.assertEqual(ctx.icons[0], b.eps["comet_db"].icon_data())
        self.assertNotEqual(ctx.icons[0], "")
        logo = HERE / "assets" / "comety.png"  # any real image will do as the game's logo
        comet.games.logo_files.return_value = [str(logo)]
        ctx.stop_game()
        self.wait_for(lambda: len(ctx.icons) == 2)
        self.assertTrue(ctx.icons[1].startswith("data:image/png;base64,"))

    def test_databases_that_appear_later_are_found(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        comet.games.gameplay_dbs.return_value = []
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        comet.games.gameplay_dbs.return_value = ["/a/gameplay.db"]
        self.wait_for(lambda: ctx.shown)
        self.assertEqual((popen.call_count, ctx.shown), (1, ["Comet - TOEM"]))

    def test_it_gives_up_after_the_retries(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"}, dbs=())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count == len(comet.GAME_WAITS))
        time.sleep(0.2)
        self.assertEqual(comet.games.gameplay_dbs.call_count, len(comet.GAME_WAITS))
        popen.assert_not_called()

    def test_endpoint_off_no_comety_and_back_on_it_starts(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        ctx.settings.values["endpoint_comet_db"] = False
        b.setup(ctx)
        self.addCleanup(b.teardown)
        time.sleep(0.2)
        popen.assert_not_called()
        comet.games.gameplay_dbs.assert_not_called()
        self.assertEqual((self.details(b), ctx.shown), ([], []))
        self.assertTrue(b.test())  # the rest of Cometa works
        ctx.shown.clear()
        ctx.settings.change("endpoint_comet_db", True)  # the game is still running: connects to it, saying nothing else
        self.wait_for(lambda: popen.call_count == 2 and ctx.shown)
        self.assertEqual(ctx.shown, ["Comet - TOEM"])
        ctx.settings.change("endpoint_comet_db", False)  # and off again: they stop, and it says so
        self.wait_for(lambda: self.details(b) == [])
        self.assertEqual(popen.return_value.terminate.call_count, 2)
        self.assertEqual(ctx.bodies[-1], "Cometa disconnected from TOEM")

    def test_a_game_that_starts_later_is_followed_through_events(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        game = {"appid": "123", "name": "TOEM", "shortcut": False}
        ctx.game.running = game
        ctx.events.emit("game_started", game)
        ctx.events.emit("game_started", game)  # the same game again (already running at start): one search
        self.wait_for(lambda: popen.call_count == 2)
        time.sleep(0.1)
        self.assertEqual((popen.call_count, ctx.shown), (2, ["Comet - TOEM"]))
        ctx.events.emit("game_stopped", {"appid": "999", "name": "other", "shortcut": False})  # not this one
        time.sleep(0.05)
        self.assertEqual(ctx.shown, ["Comet - TOEM"])
        ctx.stop_game()
        self.assertEqual(ctx.bodies[-1], "Cometa disconnected from TOEM")

    def test_a_game_that_ends_while_looking_stops_the_search(self):
        b, comet, ctx, popen, _ = self.started({"appid": "5", "name": "x"}, dbs=())
        comet.GAME_WAITS = (0.2, 0.2, 0.2)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        ctx.stop_game()
        comet.games.gameplay_dbs.return_value = ["/a/gameplay.db"]
        time.sleep(0.5)
        comet.games.gameplay_dbs.assert_not_called()
        self.assertEqual((popen.call_count, ctx.shown), (0, []))

    def test_it_listens_only_while_on(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        counts = lambda: {k: len(v) for k, v in ctx.events.cbs.items()}
        # Comet follows the games being played; GOG Comet also the selected one
        self.assertEqual(counts(), {"game_started": 2, "game_stopped": 2, "game_selected": 1})
        ctx.settings.change("endpoint_comet_db", False)
        self.assertEqual(counts(), {"game_started": 1, "game_stopped": 1, "game_selected": 1})
        ctx.settings.change("endpoint_comet_db", True)
        self.assertEqual(counts(), {"game_started": 2, "game_stopped": 2, "game_selected": 1})
        ctx.settings.change("endpoint_comet", False)
        self.assertEqual(counts(), {"game_started": 1, "game_stopped": 1, "game_selected": 0})

    def test_a_notification_without_an_image_gets_comety_s(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        endpoints = submodule(b, "endpoints")

        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", b.status()["port"], timeout=5)
            c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        when = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        self.assertEqual(post("/cometa/db", {"name": "Won", "unlock_time": when}), 200)  # Comet's own
        self.assertEqual(post("/cometa/db", {"name": "Won", "unlock_time": when,
                                                "image_url_unlocked": "https://x.example/b.png"}), 200)
        self.assertEqual(ctx.icons, [b.eps["comet_db"].icon_data(), "https://x.example/b.png"])
        self.assertEqual(b.eps["comet_db"].icon_data(), endpoints.default_icon())  # comety.png is Cometa's image
        self.assertTrue(b.test())
        self.assertEqual(ctx.icons[-1], endpoints.default_icon())

    def test_a_comet_achievement_plays_the_trophy_sound(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        when = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        c = http.client.HTTPConnection("127.0.0.1", b.status()["port"], timeout=5)
        c.request("POST", "/cometa/db", json.dumps({"name": "Won", "unlock_time": when}), {"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 200)
        c.close()
        self.assertEqual(ctx.sounds, ["trophy"])

    def test_no_game_no_comety(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        time.sleep(0.1)
        popen.assert_not_called()
        self.assertEqual((self.details(b), ctx.shown), (["Comety: waiting for a game with Comet"], []))

    def test_a_game_without_databases_shows_nothing(self):
        b, comet, ctx, popen, _ = self.started({"appid": "5", "name": "x"}, dbs=())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        self.assertEqual(ctx.shown, [])

    def test_comety_failing_doesnt_stop_the_service(self):
        b, comet, ctx, popen, _ = self.started({"appid": "5", "name": "x"})
        popen.side_effect = FileNotFoundError(2, "No such file")
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.called)
        self.assertTrue(b.status()["running"])
        self.assertFalse(any(line.startswith("●") for line in self.details(b)))

    def test_turning_an_endpoint_on_or_off_says_nothing_about_itself(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        for key in ("endpoint_comet", "endpoint_comet_db"):  # Comet speaks about games, not about itself
            ctx.settings.change(key, False)
            ctx.settings.change(key, True)
        self.assertEqual(ctx.shown, [])

    def test_every_endpoint_has_its_setting(self):
        b = load_backend()
        settings = defaults()
        self.assertEqual({name: settings.get(f"endpoint_{name}") for name in b.endpoints_mod.build()},
                         {"comet": True, "comet_db": False})  # the old method (by db) is off by default

    def test_port_taken_is_reported_not_raised(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            b = load_backend()
            b.setup(FakeCtx(busy.getsockname()[1]))
            self.addCleanup(b.teardown)
            st = b.status()
            self.assertFalse(st["running"])
            self.assertIn("can't listen", st["error"])


if __name__ == "__main__":
    unittest.main()


class Comet(unittest.TestCase):
    """GOG Comet: the config of Comet is set when a GOG game is selected, and Comet is watched when it starts."""
    GAME = {"appid": "123", "name": "Blade of Darkness", "shortcut": True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "comet", "config.toml")
        self.b = load_backend()
        self.gog = submodule(self.b, "endpoint_comet")
        self.gog.WATCH_WAIT = 0.01
        self.gog.WATCH_TRIES = 3
        self.gog.games = mock.Mock(COMET_PORT=9977, comet_config=mock.Mock(return_value=self.config),
                                   comet_pid=mock.Mock(return_value=None),
                                   comet_others=mock.Mock(return_value=[]),
                                   port_owned_by=mock.Mock(return_value=False),
                                   port_listening=mock.Mock(return_value=False))
        # Asked through the Host (backend.py)
        self.is_gog = mock.patch.object(submodule(self.b, "games"), "is_gog_launcher", return_value=True).start()
        mock.patch.object(self.b.Host, "logo", return_value="").start()
        self.addCleanup(mock.patch.stopall)
        self.ctx = FakeCtx(free_port())
        self.ctx.game.shortcut_exe = mock.Mock(return_value="/runner/Blade.bat")
        self.ctx.settings.values["endpoint_comet_db"] = False  # only GOG Comet
        self.b.setup(self.ctx)
        self.addCleanup(self.b.teardown)
        self.url = f"http://127.0.0.1:{self.b.status()['port']}/cometa"

    def wait_for(self, cond):
        for _ in range(300):
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def written(self):
        with open(self.config, encoding="utf-8") as f:
            return f.read()

    def test_selecting_a_gog_game_sets_the_config_with_cometa_s_port(self):
        self.ctx.events.emit("game_selected", self.GAME)
        self.assertIn(f'url = "{self.url}"', self.written())
        self.gog.games.comet_config.assert_called_with("/sd/steamapps/compatdata/123")
        self.ctx.game.shortcut_exe.assert_called_with("123")

    def test_other_games_are_left_alone(self):
        self.is_gog.return_value = False
        self.ctx.events.emit("game_selected", self.GAME)  # a shortcut that isn't from GOG
        self.ctx.events.emit("game_selected", {"appid": "9", "name": "Steam game", "shortcut": False})
        self.assertFalse(os.path.exists(self.config))

    def test_a_broken_config_is_not_touched(self):
        os.makedirs(os.path.dirname(self.config))
        with open(self.config, "w") as f:
            f.write("this is [not toml")
        self.ctx.events.emit("game_selected", self.GAME)
        self.assertEqual(self.written(), "this is [not toml")

    def test_comet_up_with_its_port_open_says_so_once(self):
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        time.sleep(0.1)
        self.assertEqual((self.ctx.shown, self.ctx.bodies, self.ctx.sounds),
                         (["Comet - Blade of Darkness"], ["Comet connected"], ["online"]))
        self.assertTrue(self.gog.comet_config.has_url(self.config, self.url))
        self.assertIn("● config  ● comet.exe (4321)  ● port 9977", self.b.status()["endpoints"][0]["details"][0])

    def test_it_waits_for_comet_and_then_says_so(self):
        self.gog.games.comet_pid.return_value = None
        self.ctx.events.emit("game_started", self.GAME)
        time.sleep(0.02)
        self.assertEqual(self.ctx.shown, [])
        self.gog.WATCH_TRIES = 300
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        self.wait_for(lambda: self.ctx.shown)

    def test_it_says_what_is_missing_when_it_gives_up(self):
        self.gog.games.comet_pid.return_value = 4321  # running, but the port never opens
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        self.assertEqual((self.ctx.shown, self.ctx.bodies, self.ctx.sounds),
                         (["Comet - Blade of Darkness"], ["Comet isn't ready: port 9977"], ["trophy"]))

    def test_a_config_lost_while_comet_runs_is_put_back_with_a_restart_warning(self):
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        self.gog.WATCH_WAIT = 0.05
        with mock.patch.object(self.gog.comet_config, "has_url", side_effect=[False, True]):
            self.ctx.events.emit("game_started", self.GAME)
            self.wait_for(lambda: len(self.ctx.shown) == 2)
        self.assertEqual(self.ctx.sounds, ["mention", "online"])
        self.assertIn("Restart the game", self.ctx.bodies[0])
        self.assertIn(self.url, self.written())

    def test_a_game_that_ends_stops_the_watch(self):
        self.ctx.events.emit("game_started", self.GAME)
        self.ctx.events.emit("game_stopped", self.GAME)
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        time.sleep(0.1)
        self.assertEqual(self.ctx.shown, [])

    def test_a_game_that_is_not_gog_is_not_watched(self):
        self.is_gog.return_value = False
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        self.ctx.events.emit("game_started", self.GAME)
        time.sleep(0.1)
        self.assertEqual(self.ctx.shown, [])
        self.assertEqual(self.b.status()["endpoints"][0]["details"], ["Comet: waiting for a game"])

    def test_turning_it_on_in_the_middle_of_a_session_catches_up(self):
        self.ctx.settings.change("endpoint_comet", False)
        self.ctx.game.selected = self.GAME
        self.ctx.settings.change("endpoint_comet", True)
        self.assertIn(self.url, self.written())

    def test_turning_it_off_leaves_the_config(self):
        self.ctx.events.emit("game_selected", self.GAME)
        self.ctx.settings.change("endpoint_comet", False)
        self.assertIn(self.url, self.written())
        self.assertEqual(self.ctx.shown, [])  # and says nothing

    def test_a_comet_of_another_game_holding_the_port_is_not_a_connection(self):
        self.gog.games.comet_others.return_value = ["999"]  # nothing runs for this game, another Comet does
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        self.assertEqual(self.ctx.sounds, ["trophy"])
        self.assertIn("another game (999)", self.ctx.bodies[0])
        self.assertNotIn("connected", self.ctx.bodies[0])
        self.gog.games.comet_pid.assert_called_with("123")

    def test_the_port_must_be_owned_by_the_comet_of_the_game(self):
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = False
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        self.gog.games.port_owned_by.assert_called_with(4321)
        self.assertEqual(self.ctx.bodies, ["Comet isn't ready: port 9977"])

    def test_a_port_left_open_when_the_game_starts_is_an_error(self):
        self.gog.games.port_listening.return_value = True  # another game's Comet (or anything) holds it
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        self.assertEqual((self.ctx.shown, self.ctx.bodies, self.ctx.sounds),
                         (["Comet"], ["ERROR: the game can't connect to Comet (port 9977 is in use)"], ["trophy"]))
        time.sleep(0.2)  # the watch gives up without saying it again
        self.assertEqual(len(self.ctx.shown), 1)
        self.assertIn("⚠ port in use when the game started", self.b.status()["endpoints"][0]["details"][0])

    def test_the_port_of_this_games_own_comet_is_not_an_error(self):
        self.gog.games.port_listening.return_value = True
        self.gog.games.comet_pid.return_value = 4321
        self.gog.games.port_owned_by.return_value = True
        self.ctx.events.emit("game_started", self.GAME)
        self.wait_for(lambda: self.ctx.shown)
        self.assertEqual(self.ctx.bodies, ["Comet connected"])

    def test_a_free_port_says_nothing_at_start(self):
        self.ctx.events.emit("game_started", self.GAME)
        time.sleep(0.02)
        self.assertEqual(self.ctx.shown, [])

    def test_the_port_of_a_game_that_is_not_gog_is_nobody_s_business(self):
        self.is_gog.return_value = False
        self.gog.games.port_listening.return_value = True
        self.ctx.events.emit("game_started", self.GAME)
        time.sleep(0.1)
        self.assertEqual(self.ctx.shown, [])
