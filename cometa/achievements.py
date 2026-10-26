"""Reading a game's achievements from Comet's gameplay.db. Pure stdlib, no Invasor imports.

The database is opened read-only, so Comet (which writes it while the game runs) is never blocked or changed.
"""
import os
import sqlite3
from pathlib import Path

COLUMNS = ("key", "name", "description", "visible_while_locked", "unlock_time", "image_url_locked",
           "image_url_unlocked", "rarity", "rarity_level_description")


def client_id(db_path):
    """The game's GOG client id: the folder above the user's (gameplay/<client_id>/<user_id>/gameplay.db)."""
    return os.path.basename(os.path.dirname(os.path.dirname(db_path)))


def https_url(url):
    """url if it is an https:// one, else "" (Steam only shows https images)."""
    return url if isinstance(url, str) and url.startswith("https://") else ""


def read(db_path):
    """{"client_id", "path", "unlocked", "total", "achievements": [...]}, or {"client_id", "path", "error"} if the
    database can't be read."""
    out = {"client_id": client_id(db_path), "path": db_path}
    try:
        con = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as e:
        return {**out, "error": str(e)}
    try:
        rows = con.execute(f"SELECT {', '.join(COLUMNS)} FROM achievement").fetchall()
    except sqlite3.Error as e:
        return {**out, "error": str(e)}
    finally:
        con.close()
    found = []
    for key, name, description, visible, unlock_time, locked_url, unlocked_url, rarity, level in rows:
        unlocked = unlock_time if isinstance(unlock_time, str) and unlock_time else None
        found.append({
            "key": key, "name": name or key, "description": description or "", "hidden": not visible,
            "unlocked": unlocked, "rarity": float(rarity or 0), "rarity_level": level or "",
            "image": https_url(unlocked_url) if unlocked else https_url(locked_url),
        })
    # unlocked first, the latest first (Comet's ISO times sort as text); then locked, the most common first
    unlocked = sorted((a for a in found if a["unlocked"]), key=lambda a: a["unlocked"], reverse=True)
    locked = sorted((a for a in found if not a["unlocked"]), key=lambda a: -a["rarity"])
    return {**out, "unlocked": len(unlocked), "total": len(found), "achievements": unlocked + locked}
