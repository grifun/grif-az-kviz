"""AZ-kvíz game logic: board topology, turn state machine and question pool.

Board: a triangle of 28 hexagons in 7 rows (row r has r+1 cells). A player wins
by owning a connected group of cells that touches all three sides.

Flow of one turn:
  pick   -> the player on turn names a free cell (or a black one), the referee selects it
  open   -> the referee reveals the question and starts the answer clock
  free cell:   "question" -> correct: cell is theirs
                          -> wrong:   "offer" (opponent decides whether to answer)
                                       -> declines: cell turns black, opponent picks next
                                       -> answers:  "steal"
                                                     -> correct: cell is opponent's
                                                     -> wrong:   cell turns black
  black cell:  "yesno"    -> correct: cell is theirs, wrong: opponent's
After a resolved question the turn passes to the other player, except when the opponent
won the field by answering instead of the player on turn: then that player stays on turn.
"""

from __future__ import annotations

import copy
import random
import secrets
import time

from .errors import GameError
from .questions import format_questions, initials, parse_questions, qkey, starts_with_letter, yes_no  # noqa: F401

ROWS = 7
COORDS = [(r, c) for r in range(ROWS) for c in range(r + 1)]
CELLS = len(COORDS)  # 28
INDEX = {rc: i for i, rc in enumerate(COORDS)}

EMPTY, P0, P1, BLACK = -1, 0, 1, 2

# Letters used for the final round: 28 fields, so each language gets a 28-letter list.
LETTERS = {
    "cs": ["A", "B", "C", "Č", "D", "E", "F", "G", "H", "Ch", "I", "J", "K", "L",
           "M", "N", "O", "P", "R", "Ř", "S", "Š", "T", "U", "V", "Y", "Z", "Ž"],
    "en": ["A", "B", "C", "Ch", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
           "N", "O", "P", "Q", "R", "S", "Sh", "T", "Th", "U", "V", "W", "Y", "Z"],
}
assert all(len(v) == CELLS for v in LETTERS.values())
LANGS = tuple(LETTERS)
DEFAULT_NAMES = {"cs": ["Oranžový", "Modrý"], "en": ["Orange", "Blue"]}


def _neighbors(r: int, c: int) -> list[int]:
    cand = [(r, c - 1), (r, c + 1), (r - 1, c - 1), (r - 1, c), (r + 1, c), (r + 1, c + 1)]
    return [INDEX[x] for x in cand if x in INDEX]


NEIGHBORS = [_neighbors(r, c) for r, c in COORDS]
SIDE_LEFT = frozenset(i for i, (r, c) in enumerate(COORDS) if c == 0)
SIDE_RIGHT = frozenset(i for i, (r, c) in enumerate(COORDS) if c == r)
SIDE_BOTTOM = frozenset(i for i, (r, c) in enumerate(COORDS) if r == ROWS - 1)


def winning_group(cells: list[int], player: int) -> list[int] | None:
    """Return the player's connected group touching all three sides, if any."""
    seen: set[int] = set()
    for start in range(CELLS):
        if cells[start] != player or start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            i = stack.pop()
            group.append(i)
            for n in NEIGHBORS[i]:
                if n not in seen and cells[n] == player:
                    seen.add(n)
                    stack.append(n)
        g = set(group)
        if g & SIDE_LEFT and g & SIDE_RIGHT and g & SIDE_BOTTOM:
            return sorted(group)
    return None


DEFAULT_SETTINGS = {
    "lang": "cs",              # language of both windows: "cs" or "en"
    "show_question": True,     # show question text on the players' screen
    "sound": "referee",        # where sound effects play: "referee", "board" or "off"
    "label_mode": "numbers",   # "numbers" (semifinal) or "letters" (final)
    "answer_seconds": 10,      # countdown shown when a question opens, 0 = off
}
CHOICES = {"lang": LANGS, "sound": ("referee", "board", "off"), "label_mode": ("numbers", "letters")}

HISTORY_LIMIT = 200
SNAPSHOT_FIELDS = ("cells", "turn", "phase", "active", "question", "reveal",
                   "winner", "win_cells", "tie", "used", "starting")
CUSTOM = "custom"
# The question pool is chosen on the home page; it can't change once the game exists.
SETUP_ONLY = frozenset({"select_sets", "load_questions", "clear_custom"})


class Game:
    def __init__(self, names: list[str], settings: dict, store, selected: list[str] | None = None):
        self.id = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
        self.referee_token = secrets.token_urlsafe(16)
        self.settings = dict(DEFAULT_SETTINGS)
        self._apply_settings(settings)
        defaults = DEFAULT_NAMES[self.settings["lang"]]
        self.names = [str(n).strip()[:40] or d for n, d in zip(list(names) + ["", ""], defaults)]
        self.store = store                # shared SetStore (anything with a .sets dict); sets may change live
        if selected is None:              # default: every set in the game's language
            selected = [k for k, v in store.sets.items() if v["lang"] == self.settings["lang"]]
        self.selected: list[str] = []
        self.act_select_sets(selected)
        self.custom: dict[str, list[dict]] = {"open": [], "yesno": []}
        self.used: dict[str, list[str]] = {"open": [], "yesno": []}   # keys of drawn questions
        self.history: list[dict] = []
        self.event = {"seq": 0, "type": None}
        self.timer: dict | None = None
        self.touched = time.time()
        self.starting = 0
        self._reset_board()

    @property
    def lang(self) -> str:
        return self.settings["lang"]

    # ---- state helpers -------------------------------------------------

    def _reset_board(self) -> None:
        self.cells = [EMPTY] * CELLS
        self.turn = self.starting
        self.phase = "pick"
        self.active: int | None = None
        self.question: dict | None = None
        self.reveal: dict | None = None
        self.winner: int | None = None
        self.win_cells: list[int] = []
        self.tie = False
        self.timer = None

    def _push_history(self) -> None:
        self.history.append(copy.deepcopy({f: getattr(self, f) for f in SNAPSHOT_FIELDS}))
        del self.history[:-HISTORY_LIMIT]

    def _emit(self, kind: str) -> None:
        self.event = {"seq": self.event["seq"] + 1, "type": kind}

    def _start_timer(self, seconds: float) -> None:
        self.timer = {"ends_at": time.time() + seconds, "duration": seconds} if seconds > 0 else None

    def _apply_settings(self, s: dict) -> None:
        for key, value in s.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key in CHOICES:
                if value not in CHOICES[key]:
                    raise GameError("bad_setting", key=key)
            elif key == "answer_seconds":
                value = max(0, min(120, int(value)))
            else:
                value = bool(value)
            self.settings[key] = value

    def letters(self) -> list[str]:
        return LETTERS[self.lang]

    def label(self, cell: int) -> str:
        return self.letters()[cell] if self.settings["label_mode"] == "letters" else str(cell + 1)

    # ---- question pool -------------------------------------------------

    def _pool(self, kind: str) -> list[tuple[str, dict, str]]:
        """(key, question, language) for every question in the selected sets plus custom ones."""
        out = []
        for sid in self.selected:
            s = self.store.sets.get(sid)
            if s:   # a set deleted from the folder simply drops out of the pool
                out += [(f"{sid}:{item.get('k') or qkey(item)}", item, s["lang"]) for item in s[kind]]
        out += [(f"{CUSTOM}:{kind}:{i}", item, self.lang) for i, item in enumerate(self.custom[kind])]
        return out

    def _draw(self, kind: str, cell: int, exclude: str | None = None) -> dict:
        pool = self._pool(kind)
        if not pool:
            return {"kind": kind, "key": None, "q": "", "a": "", "h": "", "open": False,
                    "note": {"code": "empty_pool"}}
        used = set(self.used[kind])
        free = [p for p in pool if p[0] not in used and p[0] != exclude]
        if not free:  # pool exhausted: start over
            self.used[kind] = []
            free = [p for p in pool if p[0] != exclude] or pool
        note = None
        if kind == "open" and self.settings["label_mode"] == "letters":
            letter = self.letters()[cell]
            matching = [p for p in free if starts_with_letter(p[1]["a"], letter, self.letters())]
            if matching:
                free = matching
            else:
                note = {"code": "no_letter", "letter": letter}
        key, item, lang = random.choice(free)
        self.used[kind].append(key)
        hint = item.get("h") or (initials(item["a"], lang) if kind == "open" else "")
        return {"kind": kind, "key": key, "q": item["q"], "a": item["a"], "h": hint, "open": False, "note": note}

    def _set_summaries(self) -> list[dict]:
        out = []
        for sid, s in self.store.sets.items():
            used = {}
            for kind in ("open", "yesno"):
                keys = {f"{sid}:{i.get('k') or qkey(i)}" for i in s[kind]}
                used[kind] = len(keys & set(self.used[kind]))
            out.append({"id": sid, "lang": s["lang"], "name": s["name"], "description": s["description"],
                        "open": len(s["open"]), "yesno": len(s["yesno"]),
                        "selected": sid in self.selected, "used": used})
        return out

    # ---- turn resolution -----------------------------------------------

    def _assign(self, cell: int, owner: int) -> None:
        self.cells[cell] = owner
        if owner in (P0, P1):
            group = winning_group(self.cells, owner)
            if group:
                self.winner, self.win_cells = owner, group

    def _finish(self, outcome: str, owner: int) -> None:
        q = self.question or {}
        self.reveal = {"cell": self.active, "label": self.label(self.active), "kind": q.get("kind", "open"),
                       "q": q.get("q", ""), "a": q.get("a", ""), "outcome": outcome, "owner": owner}
        self._assign(self.active, owner)
        self.active = None
        self.question = None
        self.timer = None
        if self.winner is not None:
            self.phase = "over"
            self._emit("win")
        else:
            self.phase = "pick"
            if outcome != "steal_correct":   # a stolen field doesn't also earn the next turn
                self.turn = 1 - self.turn
            self._emit({"black": "black", "steal_declined": "black", "yesno_wrong": "wrong"}.get(outcome, "correct"))

    def _check_winner_after_edit(self) -> None:
        self.winner, self.win_cells, self.tie = None, [], False
        for p in (P0, P1):
            group = winning_group(self.cells, p)
            if group:
                self.winner, self.win_cells = p, group
        if self.winner is not None:
            self.phase = "over"
        elif self.phase == "over":
            self.phase = "pick"

    def _require_question(self) -> None:
        if self.phase not in ("question", "offer", "steal", "yesno") or not self.question:
            raise GameError("no_question")

    # ---- actions -------------------------------------------------------

    def handle(self, action: str, data: dict, referee: bool) -> None:
        # The players' window is a passive projection; only the referee drives the game.
        if not referee:
            raise GameError("not_referee")
        if action in SETUP_ONLY:
            raise GameError("pool_locked")
        fn = getattr(self, "act_" + action, None)
        if fn is None:
            raise GameError("unknown_action", action=action)
        self.touched = time.time()
        try:
            fn(**data)
        except (TypeError, ValueError) as e:
            raise GameError("bad_data") from e

    def act_pick(self, cell: int) -> None:
        if self.phase != "pick":
            raise GameError("cannot_pick_now")
        if not isinstance(cell, int) or not 0 <= cell < CELLS:
            raise GameError("bad_cell")
        if self.cells[cell] not in (EMPTY, BLACK):
            raise GameError("cell_taken")
        self._push_history()
        self.active = cell
        self.reveal = None
        if self.cells[cell] == EMPTY:
            self.phase = "question"
            self.question = self._draw("open", cell)
        else:
            self.phase = "yesno"
            self.question = self._draw("yesno", cell)
        self.timer = None
        self._emit("pick")

    def act_open_question(self) -> None:
        """Referee reveals the question text to the players and starts the answer clock."""
        self._require_question()
        if self.question["open"]:
            raise GameError("already_open")
        self._push_history()
        self.question["open"] = True
        self._start_timer(self.settings["answer_seconds"])
        self._emit("open")

    def act_correct(self) -> None:
        self._require_question()
        if self.phase == "offer":
            raise GameError("offer_pending")
        self._push_history()
        if self.phase == "steal":
            self._finish("steal_correct", 1 - self.turn)
        else:
            self._finish("correct" if self.phase == "question" else "yesno_correct", self.turn)

    def act_wrong(self) -> None:
        self._require_question()
        if self.phase == "offer":
            raise GameError("offer_pending")
        self._push_history()
        if self.phase == "question":
            self.phase = "offer"
            self.question["open"] = True
            self.timer = None
            self._emit("wrong")
        elif self.phase == "steal":
            self._finish("black", BLACK)
        else:
            self._finish("yesno_wrong", 1 - self.turn)

    def act_steal_accept(self) -> None:
        """The opponent chooses to answer the question the player on turn missed."""
        if self.phase != "offer":
            raise GameError("no_offer")
        self._push_history()
        self.phase = "steal"
        self._start_timer(self.settings["answer_seconds"])
        self._emit("open")

    def act_steal_decline(self) -> None:
        """The opponent won't answer: the field turns black and they pick a new one."""
        if self.phase != "offer":
            raise GameError("no_offer")
        self._push_history()
        self._finish("steal_declined", BLACK)

    def act_next_question(self) -> None:
        self._require_question()
        kind, old, was_open = self.question["kind"], self.question["key"], self.question["open"]
        self.question = self._draw(kind, self.active, exclude=old)
        self.question["open"] = was_open

    def act_custom_question(self, q: str, a: str, h: str = "") -> None:
        self._require_question()
        q, a = str(q).strip()[:500], str(a).strip()[:200]
        if not q or not a:
            raise GameError("need_q_and_a")
        kind = self.question["kind"]
        if kind == "yesno":
            a = yes_no(a) or a
        hint = str(h).strip()[:20] or (initials(a, self.lang) if kind == "open" else "")
        self.question = {"kind": kind, "key": None, "q": q, "a": a, "h": hint,
                         "open": self.question["open"], "note": None}

    def act_cancel(self) -> None:
        """Player declines the field: back to picking, question returns to the pool."""
        if self.phase not in ("question", "yesno"):
            raise GameError("cancel_only_before")
        self._push_history()
        if self.question and self.question["key"] in self.used[self.question["kind"]]:
            self.used[self.question["kind"]].remove(self.question["key"])
        self.phase, self.active, self.question, self.timer = "pick", None, None, None
        self._emit("cancel")

    def act_undo(self) -> None:
        if not self.history:
            raise GameError("nothing_to_undo")
        for f, v in self.history.pop().items():
            setattr(self, f, v)
        self.timer = None
        self._emit("undo")

    def act_set_cell(self, cell: int, state: int) -> None:
        if self.phase not in ("pick", "over"):
            raise GameError("edit_only_outside")
        if not isinstance(cell, int) or not 0 <= cell < CELLS or state not in (EMPTY, P0, P1, BLACK):
            raise GameError("bad_edit")
        self._push_history()
        self.cells[cell] = state
        self._check_winner_after_edit()
        self._emit("edit")

    def act_new_round(self) -> None:
        self._push_history()
        self.starting = 1 - self.starting
        self._reset_board()
        self._emit("new_round")

    def act_end_by_count(self) -> None:
        """Time is up: the player with more cells wins."""
        if self.phase == "over":
            raise GameError("game_over_already")
        self._push_history()
        a, b = self.cells.count(P0), self.cells.count(P1)
        self.phase, self.active, self.question, self.timer = "over", None, None, None
        self.win_cells = []
        if a == b:
            self.winner, self.tie = None, True
        else:
            self.winner, self.tie = (P0 if a > b else P1), False
        self._emit("win" if not self.tie else "tie")

    def act_declare_winner(self, player: int) -> None:
        if player not in (P0, P1):
            raise GameError("bad_player")
        self._push_history()
        self.phase, self.active, self.question, self.timer = "over", None, None, None
        self.winner, self.tie = player, False
        self._emit("win")

    def act_set_names(self, names: list[str]) -> None:
        if not isinstance(names, list) or len(names) != 2:
            raise GameError("two_names")
        self.names = [str(n).strip()[:40] or self.names[i] for i, n in enumerate(names)]

    def act_set_settings(self, settings: dict) -> None:
        old_lang = self.lang
        self._apply_settings(settings)
        if self.lang != old_lang:  # keep untouched default names in the new language
            for i in (0, 1):
                if self.names[i] == DEFAULT_NAMES[old_lang][i]:
                    self.names[i] = DEFAULT_NAMES[self.lang][i]

    def act_timer_start(self, seconds: float) -> None:
        self._start_timer(max(1, min(600, float(seconds))))

    def act_timer_stop(self) -> None:
        self.timer = None

    def act_select_sets(self, sets: list[str]) -> None:
        """Choose which built-in question sets make up this game's pool."""
        if not isinstance(sets, list):
            raise GameError("bad_data")
        for sid in sets:
            if sid not in self.store.sets:
                raise GameError("unknown_set", set=str(sid)[:40])
        self.selected = [sid for sid in self.store.sets if sid in sets]   # keep registry order

    def act_load_questions(self, text: str, replace: bool = False) -> None:
        """Add the referee's own questions; replace=True discards earlier custom questions."""
        bank = parse_questions(str(text))
        if not bank["open"] and not bank["yesno"]:
            raise GameError("no_questions_found")
        if replace:
            self.act_clear_custom()
        self.custom["open"] += bank["open"]
        self.custom["yesno"] += bank["yesno"]

    def act_clear_custom(self) -> None:
        self.custom = {"open": [], "yesno": []}
        for kind in self.used:
            self.used[kind] = [k for k in self.used[kind] if not k.startswith(CUSTOM + ":")]

    # ---- views ---------------------------------------------------------

    def view(self, referee: bool) -> dict:
        q = None
        if self.question:
            q = {"kind": self.question["kind"], "h": self.question["h"], "open": self.question["open"]}
            if referee or (self.settings["show_question"] and self.question["open"]):
                q["q"] = self.question["q"]
            if referee:
                q["a"] = self.question["a"]
                q["note"] = self.question["note"]
        out = {
            "id": self.id,
            "names": self.names,
            "cells": self.cells,
            "labels": [self.label(i) for i in range(CELLS)],
            "turn": self.turn,
            "phase": self.phase,
            "active": self.active,
            "question": q,
            "reveal": self.reveal,
            "winner": self.winner,
            "win_cells": self.win_cells,
            "tie": self.tie,
            "counts": [self.cells.count(P0), self.cells.count(P1)],
            "settings": self.settings,
            "timer": self.timer,
            "event": self.event,
            "now": time.time(),
        }
        if referee:
            out["pool"] = {}
            for kind in ("open", "yesno"):
                keys = {p[0] for p in self._pool(kind)}
                out["pool"][kind] = {"total": len(keys), "used": len(keys & set(self.used[kind]))}
            out["sets"] = self._set_summaries()
            out["custom"] = {"open": len(self.custom["open"]), "yesno": len(self.custom["yesno"]),
                             "text": format_questions(self.custom, self.lang)}
            out["can_undo"] = bool(self.history)
        return out
