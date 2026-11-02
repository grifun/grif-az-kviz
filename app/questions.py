"""Question sets (one JSON file per set, in a folder or S3 bucket), pasted questions, answer initials."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from .errors import GameError
from .storage import LocalBackend, StorageError

log = logging.getLogger("azkviz.sets")

YES = {"ano", "a", "yes", "y", "true", "1"}
NO = {"ne", "n", "no", "false", "0"}
QUOTES = "(\"'„“‚"


def initials(answer: str, lang: str = "cs") -> str:
    """'Leonardo da Vinci' -> 'LdV'; in Czech 'chobotnice' -> 'Ch' (ch is one letter)."""
    out = []
    for word in answer.split():
        word = word.lstrip(QUOTES)
        if not word:
            continue
        out.append(word[:2] if lang == "cs" and word[:2].lower() == "ch" else word[0])
    if not out:
        return ""
    out[0] = out[0][0].upper() + out[0][1:]
    return "".join(out)


def starts_with_letter(answer: str, letter: str, alphabet: list[str]) -> bool:
    """True if the answer starts with `letter`, not counting longer letters such as 'Ch' for 'C'."""
    a = answer.strip().lstrip(QUOTES).lower()
    letter = letter.lower()
    if not a.startswith(letter):
        return False
    longer = [x.lower() for x in alphabet if len(x) > len(letter) and x.lower().startswith(letter)]
    return not any(a.startswith(x) for x in longer)


def yes_no(answer: str) -> str | None:
    a = str(answer).strip().lower()
    return "yes" if a in YES else "no" if a in NO else None


def parse_questions(text: str) -> dict[str, list[dict]]:
    """Parse lines 'question | answer [| hint]'. Answers ANO/NE or YES/NO go to the yes/no pool."""
    bank: dict[str, list[dict]] = {"open": [], "yesno": []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise GameError("bad_line", line=line[:60])
        q, a = parts[0][:500], parts[1][:200]
        yn = yes_no(a)
        if yn:
            bank["yesno"].append({"q": q, "a": yn})
        else:
            item = {"q": q, "a": a}
            if len(parts) > 2 and parts[2]:
                item["h"] = parts[2][:20]
            bank["open"].append(item)
    return bank


def format_questions(bank: dict[str, list[dict]], lang: str = "cs") -> str:
    """Inverse of parse_questions, used to let the referee download their custom questions."""
    yes, no = ("ANO", "NE") if lang == "cs" else ("YES", "NO")
    lines = [" | ".join([i["q"], i["a"]] + ([i["h"]] if i.get("h") else [])) for i in bank["open"]]
    lines += [f"{i['q']} | {yes if i['a'] == 'yes' else no}" for i in bank["yesno"]]
    return "\n".join(lines) + ("\n" if lines else "")


SET_LANGS = ("cs", "en")
SET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_QUESTIONS = 3000


def qkey(item: dict) -> str:
    """Stable key of a question (survives reordering and edits of *other* questions)."""
    return hashlib.sha1(f"{item['q']}\x00{item['a']}".encode()).hexdigest()[:12]


def clean_set(data: dict, sid: str | None = None) -> dict:
    """Validate and normalise a set (from a file or from the editor). Raises GameError."""
    if not isinstance(data, dict):
        raise GameError("bad_data")
    sid = str(sid if sid is not None else data.get("id", "")).strip()
    if not SET_ID.match(sid):
        raise GameError("bad_set_id")
    lang = data.get("lang", "cs")
    if lang not in SET_LANGS:
        raise GameError("bad_lang")

    def texts(value, limit):
        value = value if isinstance(value, dict) else {"cs": value, "en": value} if isinstance(value, str) else {}
        out = {l: str(value.get(l) or "").strip()[:limit] for l in SET_LANGS}
        fallback = next((v for v in out.values() if v), "")
        return {l: out[l] or fallback for l in SET_LANGS}

    name = texts(data.get("name"), 80)
    if not name["cs"]:
        raise GameError("set_name_missing")
    description = texts(data.get("description"), 200)

    open_, yesno = [], []
    if not isinstance(data.get("open") or [], list) or not isinstance(data.get("yesno") or [], list):
        raise GameError("bad_data")
    for n, item in enumerate(data.get("open") or [], 1):
        if not isinstance(item, dict):
            raise GameError("incomplete_question", n=n)
        q, a = str(item.get("q") or "").strip(), str(item.get("a") or "").strip()
        h = str(item.get("h") or "").strip()
        if not (q or a or h):
            continue                               # an empty row in the editor
        if not q or not a:
            raise GameError("incomplete_question", n=n)
        clean = {"q": q[:500], "a": a[:200]}
        if h:
            clean["h"] = h[:20]
        open_.append(clean)
    for n, item in enumerate(data.get("yesno") or [], 1):
        if not isinstance(item, dict):
            raise GameError("bad_yesno", n=n)
        q = str(item.get("q") or "").strip()
        if not q and not item.get("a"):
            continue
        yn = yes_no(item.get("a", ""))
        if not q or yn is None:
            raise GameError("bad_yesno", n=n)
        yesno.append({"q": q[:500], "a": yn})
    if len(open_) + len(yesno) > MAX_QUESTIONS:
        raise GameError("too_many", max=MAX_QUESTIONS)
    return {"id": sid, "lang": lang, "name": name, "description": description, "open": open_, "yesno": yesno}


class SetStore:
    """Question sets kept as one JSON file per set, in a local folder or an S3 bucket.

    The storage is the source of truth: files changed there (by hand, or by another server) are
    picked up by refresh(), and saves from the editor are written straight back. Thread-safe, so
    slow storage (S3) can be used from a worker thread without blocking running games.
    """

    def __init__(self, backend, min_interval: float | None = None):
        if isinstance(backend, (str, Path)):
            backend = LocalBackend(backend)
        self.backend = backend
        self.min_interval = backend.min_interval if min_interval is None else min_interval
        self.sets: dict[str, dict] = {}
        self.files: dict[str, str] = {}        # set id -> file name
        self.generation = 0     # bumped on every reload, so watchers notice changes whoever triggered them
        self._file_errors: list[dict] = []
        self._storage_error: str | None = None
        self._cache: dict[str, tuple[str, dict]] = {}   # file name -> (version token, parsed set or error)
        self._signature = None
        self._last_scan = float("-inf")
        self._lock = threading.RLock()
        self.refresh(force=True)

    @property
    def errors(self) -> list[dict]:
        out = list(self._file_errors)
        if self._storage_error:
            err = GameError("storage_error", error=self._storage_error)
            out.insert(0, {"file": self.backend.location, "message": {l: err.message(l) for l in SET_LANGS}})
        return out

    def _parse(self, name: str, token: str) -> dict:
        try:
            data = json.loads(self.backend.read(name))
            s = clean_set(data, data.get("id") or Path(name).stem)
        except GameError as e:
            return {"error": {"file": name, "message": {l: e.message(l) for l in SET_LANGS}}}
        except (StorageError, ValueError, AttributeError, TypeError) as e:
            err = GameError("bad_file", error=str(e)[:200])
            return {"error": {"file": name, "message": {l: err.message(l) for l in SET_LANGS}}}
        for kind in ("open", "yesno"):
            for item in s[kind]:
                item["k"] = qkey(item)
        s["version"], s["file"] = token, name
        return s

    def refresh(self, force: bool = False) -> bool:
        """Reload if any file was added, removed or modified. Returns True if anything changed.

        Without force, the storage is checked at most every `min_interval` seconds.
        """
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_scan < self.min_interval:
                return False
            self._last_scan = now
            try:
                listing = self.backend.list()
            except StorageError as e:
                if str(e) != self._storage_error:     # log once per new problem, keep the last good sets
                    log.warning("Question storage unavailable, keeping the last loaded sets: %s", e)
                    self._storage_error = str(e)
                    self.generation += 1
                    return True
                return False
            recovered = self._storage_error is not None
            self._storage_error = None
            signature = tuple(sorted(listing.items()))
            if signature == self._signature and not force and not recovered:
                return False
            self._signature = signature
            sets, files, errors, cache = {}, {}, [], {}
            for name in sorted(listing):
                token = listing[name]
                cached = self._cache.get(name)
                entry = cached[1] if cached and cached[0] == token else self._parse(name, token)
                cache[name] = (token, entry)
                if "error" in entry:
                    errors.append(entry["error"])
                elif entry["id"] in sets:
                    err = GameError("set_exists", set=entry["id"])
                    errors.append({"file": name, "message": {l: err.message(l) for l in SET_LANGS}})
                else:
                    sets[entry["id"]], files[entry["id"]] = entry, name
            self.sets, self.files, self._file_errors, self._cache = sets, files, errors, cache
            self.generation += 1
            return True

    def get(self, sid: str) -> dict:
        self.refresh()
        if sid not in self.sets:
            raise GameError("unknown_set", set=sid)
        return self.sets[sid]

    def save(self, sid: str, data: dict, version: str | None, who: str = "") -> dict:
        """Create (version=None) or overwrite a set. Refuses to overwrite a newer file."""
        with self._lock:
            self.refresh(force=True)          # compare against what is in the storage right now
            if self._storage_error:
                raise GameError("storage_error", error=self._storage_error)
            clean = clean_set(data, sid)
            if sid in self.sets:
                if version != self.sets[sid]["version"]:
                    raise GameError("set_changed" if version else "set_exists", set=sid)
                name, created = self.files[sid], False
            else:
                if version:
                    raise GameError("set_changed", set=sid)
                name, created = f"{sid}.json", True
                if name in self._cache:       # a file with this name holds a broken or differently-named set
                    raise GameError("set_exists", set=sid)
            text = json.dumps(clean, ensure_ascii=False, indent=1) + "\n"
            try:
                self.backend.write(name, text)
            except StorageError as e:
                log.error("Saving question set %r failed: %s", sid, e)
                raise GameError("storage_error", error=str(e)[:300]) from e
            # The full file goes to the log: a record of every edit, wherever the storage is.
            log.info("Question set %r %s by %s -> %s (%d regular, %d YES/NO). File content:\n%s",
                     sid, "created" if created else "saved", who or "?", self.backend.where(name),
                     len(clean["open"]), len(clean["yesno"]), text.rstrip("\n"))
            self.refresh(force=True)
            return self.sets[sid]

    def delete(self, sid: str, version: str | None, who: str = "") -> None:
        """Move the set's file to a .deleted/ subfolder (so a mistake can be undone by hand)."""
        with self._lock:
            self.refresh(force=True)
            if sid not in self.sets:
                raise GameError("unknown_set", set=sid)
            if version != self.sets[sid]["version"]:
                raise GameError("set_changed", set=sid)
            name = self.files[sid]
            try:
                target = self.backend.trash(name)
            except StorageError as e:
                log.error("Deleting question set %r failed: %s", sid, e)
                raise GameError("storage_error", error=str(e)[:300]) from e
            log.info("Question set %r deleted by %s: %s moved to %s", sid, who or "?", self.backend.where(name), target)
            self.refresh(force=True)


def summary(s: dict) -> dict:
    return {"id": s["id"], "lang": s["lang"], "name": s["name"], "description": s["description"],
            "open": len(s["open"]), "yesno": len(s["yesno"])}


def public(s: dict) -> dict:
    """A set as sent to the editor / preview (without internal keys)."""
    strip = lambda items: [{k: v for k, v in i.items() if k != "k"} for i in items]  # noqa: E731
    return {**{k: s[k] for k in ("id", "lang", "name", "description", "version", "file")},
            "open": strip(s["open"]), "yesno": strip(s["yesno"])}
