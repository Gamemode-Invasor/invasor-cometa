import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from tests.loader import HERE, load_backend, submodule

spec = importlib.util.spec_from_file_location("cometa_achievements_under_test", HERE / "achievements.py")
ach = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ach
spec.loader.exec_module(ach)

# Comet's own table, as it is in a real gameplay.db
SCHEMA = """CREATE TABLE `achievement` (`id` INTEGER PRIMARY KEY NOT NULL,`key` TEXT UNIQUE NOT NULL,`name` TEXT NOT NULL,
`description` TEXT NOT NULL,`visible_while_locked` INTEGER CHECK ( visible_while_locked IN ( 0, 1 ) ) NOT NULL,
`unlock_time` TEXT,`image_url_locked` TEXT NOT NULL,`image_url_unlocked` TEXT NOT NULL,
`changed` INTEGER CHECK ( changed IN ( 0, 1 ) ) NOT NULL, rarity REAL NOT NULL DEFAULT 0.0,
rarity_level_description TEXT NOT NULL DEFAULT "", rarity_level_slug TEXT NOT NULL DEFAULT "")"""


def achievement(n, key, unlock_time=None, visible=1, rarity=50.0, level="Common"):
    return (n, key, key.title(), f"Do {key}", visible, unlock_time, f"https://images.gog.com/{key}_locked.png",
            f"https://images.gog.com/{key}.png", 0, rarity, level, level.lower())


class Read(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = os.path.join(self.tmp.name, "gameplay", "47746531845925190", "46988925412590216")
        os.makedirs(folder)
        self.db = os.path.join(folder, "gameplay.db")

    def make(self, rows):
        con = sqlite3.connect(self.db)
        con.execute(SCHEMA)
        con.executemany("INSERT INTO achievement VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_unlocked_and_locked(self):
        self.make([
            achievement(1, "first", "2026-10-11T00:50:24+0000", rarity=72.6),
            achievement(2, "rare", rarity=3.1, level="Epic"),
            achievement(3, "latest", "2026-10-12T10:00:00+0000"),
            achievement(4, "common", rarity=90.0),
            achievement(5, "secret", visible=0),
        ])
        got = ach.read(self.db)
        self.assertEqual((got["client_id"], got["unlocked"], got["total"]), ("47746531845925190", 2, 5))
        # unlocked first, the latest first; then locked, the most common first
        self.assertEqual([a["key"] for a in got["achievements"]], ["latest", "first", "common", "secret", "rare"])
        first = got["achievements"][1]
        self.assertEqual(first, {"key": "first", "name": "First", "description": "Do first", "hidden": False,
                                 "unlocked": "2026-10-11T00:50:24+0000", "rarity": 72.6, "rarity_level": "Common",
                                 "image": "https://images.gog.com/first.png"})
        rare = got["achievements"][-1]
        self.assertEqual((rare["unlocked"], rare["image"]), (None, "https://images.gog.com/rare_locked.png"))
        self.assertTrue(got["achievements"][3]["hidden"])

    def test_only_https_images(self):
        row = list(achievement(1, "x"))
        row[6] = "file:///etc/passwd"
        self.make([tuple(row)])
        self.assertEqual(ach.read(self.db)["achievements"][0]["image"], "")

    def test_the_database_is_not_changed(self):
        self.make([achievement(1, "x")])
        before = os.stat(self.db).st_mtime_ns, os.listdir(os.path.dirname(self.db))
        ach.read(self.db)
        self.assertEqual((os.stat(self.db).st_mtime_ns, os.listdir(os.path.dirname(self.db))), before)

    def test_a_database_without_achievements_is_an_error(self):
        sqlite3.connect(self.db).close()
        got = ach.read(self.db)
        self.assertIn("error", got)
        self.assertEqual(got["client_id"], "47746531845925190")

    def test_a_missing_database_is_an_error(self):
        self.assertIn("error", ach.read(self.db))


class Backend(unittest.TestCase):
    def test_a_game_that_is_not_gog(self):
        b = load_backend()
        b.ctx = mock.Mock()
        b.host = b.Host(b.ctx)
        b.ctx.game.shortcut_exe.return_value = None
        self.assertEqual(b.achievements("123"), {"gog": False})
        self.assertFalse(b.is_gog("123"))

    def test_a_gog_game_with_its_databases(self):
        b = load_backend()
        b.ctx = mock.Mock()
        b.host = b.Host(b.ctx)
        games = submodule(b, "games")
        with mock.patch.object(games, "is_gog_launcher", return_value=True), \
                mock.patch.object(games, "gameplay_dbs", return_value=[]) as dbs:
            self.assertEqual(b.achievements("123"), {"gog": True, "dbs": []})
        b.ctx.paths.compatdata.assert_called_with("123")  # the library that holds the game, not only the main one
        dbs.assert_called_with(b.ctx.paths.compatdata.return_value)


if __name__ == "__main__":
    unittest.main()
