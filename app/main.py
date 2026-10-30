"""AZ-kvíz web server: HTTP pages + one WebSocket channel per game.

All state lives in memory, so run a single process (one worker).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .errors import GameError
from .game import Game
from .questions import SetStore, public, summary

# App log (set edits etc.). Uvicorn only sets up its own loggers, so ours gets a handler here,
# printing to stderr in the same style as uvicorn's lines.
_log = logging.getLogger("azkviz")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
# Question sets: one JSON file per set in this folder (edited by hand or in the /sets editor).
SETS_DIR = Path(os.environ.get("AZKVIZ_SETS_DIR") or ROOT.parent / "question_sets").resolve()
STORE = SetStore(SETS_DIR)
# Editing sets: with a password set, anyone who knows it may edit; without one, only the
# computer running the server (localhost) may edit.
EDIT_PASSWORD = os.environ.get("AZKVIZ_EDIT_PASSWORD", "")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
WATCH_SECONDS = 3


def _both(code: str) -> str:
    """Message in both languages, for errors that happen before we know the game's language."""
    e = GameError(code)
    return f"{e.message('cs')} / {e.message('en')}"

MAX_GAMES = 500
GAME_TTL = 12 * 3600  # drop games idle for 12 hours

games: dict[str, Game] = {}
sockets: dict[str, set[tuple[WebSocket, bool]]] = {}


async def _watch_sets_loop() -> None:
    """Pick up set files added, edited or removed on disk and push the change to open games."""
    seen = STORE.generation
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        STORE.refresh()
        if STORE.generation != seen:   # changed on disk, or reloaded by an API call in the meantime
            seen = STORE.generation
            await broadcast_all()


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - GAME_TTL
        for gid in [g.id for g in games.values() if g.touched < cutoff and not sockets.get(g.id)]:
            games.pop(gid, None)
            sockets.pop(gid, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(_cleanup_loop()), asyncio.create_task(_watch_sets_loop())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="AZ-kvíz", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_pages_and_assets(request: Request, call_next):
    """Make browsers re-check pages and static files on every load (a cheap 304 when unchanged),
    so an update to the app is never hidden behind a stale cached script."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def revalidate(request: Request, call_next):
    # Make browsers check for a newer copy every time, so an update never runs with stale JS.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


class NewGame(BaseModel):
    names: list[str] = []
    settings: dict = {}
    sets: list[str] | None = None  # built-in question sets; None = all sets in the game's language
    questions: str = ""            # optional custom questions added to the pool


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "games": len(games)}


def _lang(request: Request) -> str:
    return "en" if request.headers.get("x-lang") == "en" else "cs"


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _can_edit(request: Request) -> bool:
    if EDIT_PASSWORD:
        return secrets.compare_digest(request.headers.get("x-edit-password", ""), EDIT_PASSWORD)
    return bool(request.client) and request.client.host in LOCAL_HOSTS


def _require_editor(request: Request) -> None:
    if not _can_edit(request):
        code = "edit_password" if EDIT_PASSWORD else "edit_forbidden"
        raise HTTPException(403, GameError(code).message(_lang(request)))


@app.get("/sets")
async def sets_page():
    return FileResponse(STATIC / "sets.html")


@app.get("/api/sets")
async def list_sets():
    STORE.refresh()   # (the watcher broadcasts any change this picks up)
    return [summary(s) for s in STORE.sets.values()]


@app.get("/api/sets/{sid}")
async def get_set(sid: str, request: Request):
    """Full content of a set (used by the preview and the editor)."""
    try:
        return public(STORE.get(sid))
    except GameError as e:
        raise HTTPException(404, e.message(_lang(request)))


@app.get("/api/editor")
async def editor_status(request: Request):
    STORE.refresh()
    return {"can_edit": _can_edit(request), "needs_password": bool(EDIT_PASSWORD), "dir": str(SETS_DIR),
            "errors": STORE.errors}


class SaveSet(BaseModel):
    set: dict
    version: str | None = None   # None = create a new set; otherwise the version that was loaded


@app.put("/api/sets/{sid}")
async def save_set(sid: str, body: SaveSet, request: Request):
    _require_editor(request)
    try:
        saved = STORE.save(sid, body.set, body.version, who=_client(request))
    except GameError as e:
        status = 409 if e.code in ("set_changed", "set_exists") else 400
        raise HTTPException(status, e.message(_lang(request)))
    await broadcast_all()
    return public(saved)


@app.delete("/api/sets/{sid}")
async def delete_set(sid: str, request: Request, version: str = ""):
    _require_editor(request)
    try:
        STORE.delete(sid, version, who=_client(request))
    except GameError as e:
        status = {"unknown_set": 404, "set_changed": 409}.get(e.code, 400)
        raise HTTPException(status, e.message(_lang(request)))
    await broadcast_all()
    return {"ok": True}


@app.post("/api/sets/reload")
async def reload_sets():
    """Re-read the folder now (it is also watched every few seconds)."""
    if STORE.refresh(force=True):
        await broadcast_all()
    return {"sets": len(STORE.sets), "errors": STORE.errors}


@app.post("/api/games")
async def create_game(body: NewGame):
    if len(games) >= MAX_GAMES:
        oldest = min(games.values(), key=lambda g: g.touched)
        games.pop(oldest.id, None)
        sockets.pop(oldest.id, None)
    names = (body.names + ["", ""])[:2]
    lang = body.settings.get("lang", "cs")
    try:
        STORE.refresh()
        game = Game(names, body.settings, STORE, body.sets)
        if body.questions.strip():
            game.act_load_questions(body.questions)
    except GameError as e:
        raise HTTPException(400, e.message(lang))
    except (ValueError, TypeError):
        raise HTTPException(400, GameError("bad_data").message(lang))
    while game.id in games:
        game.id = Game(names, {}, STORE, []).id
    games[game.id] = game
    return {"id": game.id, "token": game.referee_token}


def _game_or_404(gid: str) -> Game:
    game = games.get(gid)
    if not game:
        raise HTTPException(404, _both("game_not_found"))
    return game


@app.get("/g/{gid}")
async def board_page(gid: str):
    _game_or_404(gid)
    return FileResponse(STATIC / "board.html")


@app.get("/g/{gid}/referee")
async def referee_page(gid: str):
    _game_or_404(gid)
    return FileResponse(STATIC / "referee.html")


async def _send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception:
        return False


async def broadcast_all() -> None:
    await asyncio.gather(*(broadcast(g) for g in list(games.values()) if sockets.get(g.id)))


async def broadcast(game: Game) -> None:
    conns = list(sockets.get(game.id, ()))
    views = {True: {"type": "state", "state": game.view(True)},
             False: {"type": "state", "state": game.view(False)}}
    results = await asyncio.gather(*(_send(ws, views[ref]) for ws, ref in conns))
    for (ws, ref), ok in zip(conns, results):
        if not ok:
            sockets.get(game.id, set()).discard((ws, ref))


@app.websocket("/ws/{gid}")
async def ws_endpoint(ws: WebSocket, gid: str, t: str = ""):
    game = games.get(gid)
    await ws.accept()
    if not game:
        await ws.send_text(json.dumps({"type": "error", "fatal": True, "message": _both("game_not_found")},
                                      ensure_ascii=False))
        await ws.close()
        return
    referee = bool(t) and secrets.compare_digest(t, game.referee_token)
    if t and not referee:
        await ws.send_text(json.dumps({"type": "error", "fatal": True, "message": GameError("bad_referee_link").message(game.lang)},
                                      ensure_ascii=False))
        await ws.close()
        return
    entry = (ws, referee)
    sockets.setdefault(gid, set()).add(entry)
    await _send(ws, {"type": "state", "state": game.view(referee)})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                action = str(msg.get("action", ""))
                if action == "ping":
                    await _send(ws, {"type": "pong"})
                    continue
                data = msg.get("data") or {}
                if not isinstance(data, dict):
                    raise GameError("bad_data")
                game.handle(action, data, referee)
            except GameError as e:
                await _send(ws, {"type": "error", "message": e.message(game.lang)})
                continue
            except (ValueError, TypeError):
                await _send(ws, {"type": "error", "message": GameError("bad_data").message(game.lang)})
                continue
            await broadcast(game)
    except WebSocketDisconnect:
        pass
    finally:
        sockets.get(gid, set()).discard(entry)
