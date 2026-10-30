import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import game as g
from app import main
from app.main import app
from app.questions import SetStore


def _set(sid, lang, answers=(), yesno=()):
    return {"id": sid, "lang": lang, "name": {"cs": sid, "en": sid}, "description": {},
            "open": [{"q": f"{sid}-Q{i}", "a": a} for i, a in enumerate(answers)],
            "yesno": [{"q": f"{sid}-YN{i}", "a": a} for i, a in enumerate(yesno)]}


SETS = {
    "cs-a": _set("cs-a", "cs", ["Anna", "Brno", "Leonardo da Vinci", "Cyril", "chobotnice"], ["yes", "no"]),
    "en-a": _set("en-a", "en", ["Charles Darwin", "Thames", "Shakespeare"], ["yes"]),
}


def new_game(sets=None, **settings):
    return g.Game(["", ""], settings, SimpleNamespace(sets=SETS), sets)


def test_topology():
    assert g.CELLS == 28
    assert len(g.SIDE_LEFT) == len(g.SIDE_RIGHT) == len(g.SIDE_BOTTOM) == 7
    assert sorted(g.NEIGHBORS[0]) == [1, 2]                 # apex
    assert len(g.NEIGHBORS[g.INDEX[(3, 1)]]) == 6           # interior cell
    for i, ns in enumerate(g.NEIGHBORS):                    # symmetric
        assert all(i in g.NEIGHBORS[n] for n in ns)


def test_win_detection():
    cells = [g.EMPTY] * g.CELLS
    left_edge = [g.INDEX[(r, 0)] for r in range(7)]         # touches left + bottom (corner) + right (apex)
    for i in left_edge:
        cells[i] = g.P0
    assert g.winning_group(cells, g.P0) == sorted(left_edge)
    cells[g.INDEX[(0, 0)]] = g.EMPTY                         # without apex no right side
    assert g.winning_group(cells, g.P0) is None
    # single corner cells don't connect
    cells = [g.EMPTY] * g.CELLS
    for rc in [(0, 0), (6, 0), (6, 6)]:
        cells[g.INDEX[rc]] = g.P1
    assert g.winning_group(cells, g.P1) is None


def test_initials_and_letters():
    cs, en = g.LETTERS["cs"], g.LETTERS["en"]
    assert g.initials("Leonardo da Vinci") == "LdV"
    assert g.initials("chobotnice") == "Ch"
    assert g.initials("Charles Darwin", "en") == "CD"
    assert g.initials("plejtvák obrovský") == "Po"
    assert g.starts_with_letter("Cyril", "C", cs)
    assert not g.starts_with_letter("chobotnice", "C", cs)
    assert g.starts_with_letter("chobotnice", "Ch", cs)
    assert not g.starts_with_letter("Thames", "T", en) and g.starts_with_letter("Thames", "Th", en)


def test_default_pool_follows_language_and_selection():
    assert new_game().selected == ["cs-a"]
    assert new_game(lang="en").selected == ["en-a"]
    game = new_game(lang="en")
    assert game.names == ["Orange", "Blue"]
    game.act_pick(cell=0)
    assert game.question["key"].startswith("en-a:")
    with pytest.raises(g.GameError) as e:
        game.act_select_sets(sets=["nope"])
    assert "Unknown question set" in e.value.message("en")


def test_select_sets_and_custom_questions():
    game = new_game(sets=[])
    game.act_pick(cell=0)
    assert game.question["note"] == {"code": "empty_pool"}
    game.act_cancel()
    game.act_load_questions(text="Moje otázka? | Odpověď\nJe to tak? | ano")
    view = game.view(True)
    assert view["pool"] == {"open": {"total": 1, "used": 0}, "yesno": {"total": 1, "used": 0}}
    assert view["custom"]["text"] == "Moje otázka? | Odpověď\nJe to tak? | ANO\n"
    game.act_pick(cell=0)
    assert game.question["key"] == "custom:open:0" and game.question["h"] == "O"
    game.act_correct()
    game.act_select_sets(sets=["cs-a", "en-a"])
    summary = {s["id"]: s for s in game.view(True)["sets"]}
    assert summary["cs-a"]["selected"] and summary["en-a"]["selected"]
    assert game.view(True)["pool"]["open"] == {"total": 9, "used": 1}
    game.act_clear_custom()
    assert game.view(True)["pool"]["open"] == {"total": 8, "used": 0}


def test_language_switch_renames_default_names_and_letters():
    game = new_game(label_mode="letters")
    game.act_set_names(names=["Eva", ""])
    game.act_set_settings(settings={"lang": "en"})
    assert game.names == ["Eva", "Blue"]
    assert game.label(3) == "Ch" and game.label(17) == "Q"


def test_builtin_sets_are_valid():
    sets = SetStore(main.SETS_DIR).sets
    assert {s["lang"] for s in sets.values()} == {"cs", "en"}
    for lang in ("cs", "en"):
        mine = [s for s in sets.values() if s["lang"] == lang]
        assert sum(len(s["open"]) for s in mine) >= 100 and sum(len(s["yesno"]) for s in mine) >= 40
    for s in sets.values():
        assert s["name"]["cs"] and s["name"]["en"]
        assert all(q["q"] and q["a"] for q in s["open"])
        assert all(q["a"] in ("yes", "no") for q in s["yesno"])


def test_correct_wrong_steal_black_flow():
    game = new_game()
    game.act_pick(cell=0)
    assert game.phase == "question" and game.question["kind"] == "open"
    game.act_correct()
    assert game.cells[0] == g.P0 and game.turn == 1 and game.phase == "pick"

    game.act_pick(cell=1)                    # B wrong, A steals the field...
    game.act_wrong()
    assert game.phase == "offer" and game.timer is None
    with pytest.raises(g.GameError):
        game.act_correct()                   # the opponent must first choose to answer
    game.act_steal_accept()
    assert game.phase == "steal"
    game.act_correct()
    assert game.cells[1] == g.P0 and game.turn == 1   # ...but B stays on turn

    game.act_pick(cell=2)                    # B again; nobody knows -> black, turn passes
    game.act_wrong()
    game.act_steal_accept()
    game.act_wrong()
    assert game.cells[2] == g.BLACK and game.turn == 0

    game.act_pick(cell=2)                    # A picks the black cell -> yes/no question
    assert game.phase == "yesno" and game.question["kind"] == "yesno"
    game.act_wrong()                         # wrong -> opponent gets it
    assert game.cells[2] == g.P1 and game.turn == 1
    assert game.reveal["outcome"] == "yesno_wrong"


def test_cannot_pick_owned_and_undo():
    game = new_game()
    game.act_pick(cell=5)
    game.act_correct()
    with pytest.raises(g.GameError):
        game.act_pick(cell=5)
    game.act_undo()
    assert game.phase == "question" and game.cells[5] == g.EMPTY
    game.act_undo()
    assert game.phase == "pick" and game.active is None and game.used["open"] == []


def test_full_game_win():
    game = new_game()
    left = [g.INDEX[(r, 0)] for r in range(7)]
    other = [i for i in range(g.CELLS) if i not in left]
    for k, cell in enumerate(left):
        game.act_pick(cell=cell)            # A
        game.act_correct()
        if k < len(left) - 1:
            game.act_pick(cell=other[k])    # B answers wrong, A misses too -> black
            game.act_wrong()
            game.act_steal_accept()
            game.act_wrong()
    assert game.phase == "over" and game.winner == 0 and game.win_cells == sorted(left)


def test_end_by_count_and_tie():
    game = new_game()
    game.act_end_by_count()
    assert game.tie and game.winner is None
    game.act_new_round()
    assert game.turn == 1 and game.phase == "pick"


def test_letters_mode_prefers_matching_answer():
    game = new_game(label_mode="letters")
    cell = g.LETTERS["cs"].index("Ch")
    game.act_pick(cell=cell)
    assert game.question["a"] == "chobotnice"


def test_view_hides_answer_from_players():
    game = new_game(show_question=False)
    game.act_pick(cell=0)
    game.act_open_question()
    pv, rv = game.view(False), game.view(True)
    assert "a" not in pv["question"] and "q" not in pv["question"] and pv["question"]["h"]
    assert rv["question"]["a"]


def test_question_text_hidden_until_referee_opens_it():
    game = new_game()
    game.act_pick(cell=0)
    assert game.timer is None
    assert "q" not in game.view(False)["question"] and game.view(True)["question"]["q"]
    game.act_open_question()
    assert game.view(False)["question"]["q"] and game.timer is not None
    with pytest.raises(g.GameError):
        game.act_open_question()
    game.act_undo()
    assert not game.question["open"]
    game.act_open_question()
    game.act_wrong()                          # the question stays visible, clock stops until the opponent decides
    assert game.view(False)["question"]["open"] and game.timer is None
    game.act_steal_accept()
    assert game.timer is not None


def test_opponent_declines_to_answer():
    game = new_game()
    game.act_pick(cell=3)
    game.act_open_question()
    game.act_wrong()
    game.act_steal_decline()                  # opponent passes: field turns black, they pick next
    assert game.cells[3] == g.BLACK and game.turn == 1 and game.phase == "pick"
    assert game.reveal["outcome"] == "steal_declined" and game.event["type"] == "black"
    game.act_undo()
    assert game.phase == "offer"
    with pytest.raises(g.GameError):
        game.act_cancel()


def test_question_pool_is_locked_during_the_game():
    game = new_game(sets=["cs-a"])
    for action, data in [("select_sets", {"sets": []}), ("load_questions", {"text": "Q? | A"}), ("clear_custom", {})]:
        with pytest.raises(g.GameError) as e:
            game.handle(action, data, referee=True)
        assert e.value.code == "pool_locked"
    assert game.selected == ["cs-a"]


def test_players_window_cannot_control_anything():
    game = new_game()
    for action, data in [("pick", {"cell": 0}), ("correct", {}), ("undo", {})]:
        with pytest.raises(g.GameError):
            game.handle(action, data, referee=False)
    game.handle("pick", {"cell": 0}, referee=True)
    assert game.phase == "question"


def test_parse_questions():
    bank = g.parse_questions("# komentář\nOtázka 1 | Praha\nJe to pravda? | ano\nIs it? | NO\nX | Karel IV. | K. IV.\n")
    assert bank["open"][0] == {"q": "Otázka 1", "a": "Praha"}
    assert bank["yesno"] == [{"q": "Je to pravda?", "a": "yes"}, {"q": "Is it?", "a": "no"}]
    assert bank["open"][1]["h"] == "K. IV."
    with pytest.raises(g.GameError):
        g.parse_questions("bez oddělovače")


def test_http_and_websocket_roundtrip():
    client = TestClient(app)
    sets = client.get("/api/sets").json()
    assert any(s["id"] == "en-yes-no" and s["yesno"] == 40 for s in sets)
    assert client.get("/api/sets/cs-svet").json()["open"]
    res = client.post("/api/games", json={"names": ["Eva", "Petr"], "sets": ["cs-svet", "cs-ano-ne"]})
    assert res.status_code == 200
    gid, token = res.json()["id"], res.json()["token"]
    assert client.get(f"/g/{gid}").status_code == 200
    assert client.get(f"/g/{gid}/referee").status_code == 200
    assert client.get("/g/nope").status_code == 404

    with client.websocket_connect(f"/ws/{gid}?t={token}") as ref, client.websocket_connect(f"/ws/{gid}") as board:
        assert ref.receive_json()["state"]["names"] == ["Eva", "Petr"]
        board.receive_json()
        board.send_json({"action": "pick", "data": {"cell": 13}})
        assert board.receive_json()["type"] == "error"
        ref.send_json({"action": "pick", "data": {"cell": 13}})
        s_ref, s_board = ref.receive_json()["state"], board.receive_json()["state"]
        assert s_ref["phase"] == "question" and s_ref["question"]["a"]
        assert "a" not in s_board["question"]
        ref.send_json({"action": "open_question"})
        assert "q" in board.receive_json()["state"]["question"]
        ref.receive_json()
        ref.send_json({"action": "correct"})
        assert ref.receive_json()["state"]["cells"][13] == 0

    with client.websocket_connect(f"/ws/{gid}?t=wrong") as bad:
        msg = bad.receive_json()
        assert msg["type"] == "error" and msg["fatal"]


# ---- question sets stored as files ----

def _payload(name="Moje sada", lang="cs", open_=(("Otázka?", "Odpověď"),), yesno=(("Je to tak?", "ANO"),)):
    return {"lang": lang, "name": {"cs": name, "en": ""}, "description": {"cs": "", "en": ""},
            "open": [{"q": q, "a": a} for q, a in open_], "yesno": [{"q": q, "a": a} for q, a in yesno]}


def test_store_create_update_delete(tmp_path):
    store = SetStore(tmp_path)
    s = store.save("moje", _payload(), None)
    data = json.loads((tmp_path / "moje.json").read_text(encoding="utf-8"))
    assert data["name"] == {"cs": "Moje sada", "en": "Moje sada"}      # missing name filled in
    assert data["yesno"] == [{"q": "Je to tak?", "a": "yes"}] and "k" not in data["open"][0]
    with pytest.raises(g.GameError) as e:                                # creating twice
        store.save("moje", _payload(), None)
    assert e.value.code == "set_exists"
    s2 = store.save("moje", _payload(open_=(("A?", "a"), ("B?", "b"))), s["version"])
    assert len(s2["open"]) == 2
    with pytest.raises(g.GameError) as e:                                # stale version
        store.save("moje", _payload(), s["version"])
    assert e.value.code == "set_changed"
    store.delete("moje", s2["version"])
    assert not (tmp_path / "moje.json").exists() and "moje" not in store.sets
    assert len(list((tmp_path / ".deleted").glob("moje-*.json"))) == 1


def test_store_validation(tmp_path):
    store = SetStore(tmp_path)
    for sid, payload, code in [
        ("Bad ID", _payload(), "bad_set_id"),
        ("x", {**_payload(), "name": {"cs": "", "en": ""}}, "set_name_missing"),
        ("x", _payload(open_=(("Otázka bez odpovědi", ""),)), "incomplete_question"),
        ("x", _payload(yesno=(("Možná?", "možná"),)), "bad_yesno"),
        ("x", {**_payload(), "lang": "de"}, "bad_lang"),
    ]:
        with pytest.raises(g.GameError) as e:
            store.save(sid, payload, None)
        assert e.value.code == code
    s = store.save("x", _payload(open_=(("Q?", "A"), ("", ""))), None)   # empty editor rows are dropped
    assert len(s["open"]) == 1


def test_store_picks_up_hand_edits_and_reports_bad_files(tmp_path):
    store = SetStore(tmp_path)
    assert store.sets == {}
    (tmp_path / "rucni.json").write_text(json.dumps({"id": "rucni", "name": "Ruční", "open": [{"q": "Q", "a": "A"}]}),
                                         encoding="utf-8")
    (tmp_path / "rozbita.json").write_text("{ not json", encoding="utf-8")
    assert store.refresh() is True
    assert list(store.sets) == ["rucni"] and store.sets["rucni"]["lang"] == "cs"
    assert [e["file"] for e in store.errors] == ["rozbita.json"]
    assert store.refresh() is False                                      # nothing changed since


def test_games_see_live_set_changes(tmp_path):
    store = SetStore(tmp_path)
    s = store.save("live", _payload(open_=(("Q1?", "A1"),)), None)
    game = g.Game(["", ""], {}, store, ["live"])
    game.act_pick(cell=0)
    game.act_correct()
    assert game.view(True)["pool"]["open"] == {"total": 1, "used": 1}
    s = store.save("live", _payload(open_=(("Q1?", "A1"), ("Q2?", "A2"))), s["version"])
    assert game.view(True)["pool"]["open"] == {"total": 2, "used": 1}    # the used question stays used
    game.act_pick(cell=1)
    assert game.question["q"] == "Q2?"
    game.act_cancel()
    store.delete("live", s["version"])
    assert game.view(True)["pool"]["open"]["total"] == 0


def test_set_editing_api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STORE", SetStore(tmp_path))
    client = TestClient(app)
    payload = _payload()
    # TestClient is not localhost and no password is set -> editing refused
    assert client.put("/api/sets/api-set", json={"set": payload}).status_code == 403
    monkeypatch.setattr(main, "EDIT_PASSWORD", "tajne")
    assert client.get("/api/editor").json()["can_edit"] is False
    assert client.put("/api/sets/api-set", json={"set": payload}, headers={"x-edit-password": "spatne"}).status_code == 403
    h = {"x-edit-password": "tajne", "x-lang": "en"}
    assert client.get("/api/editor", headers=h).json()["can_edit"] is True
    res = client.put("/api/sets/api-set", json={"set": payload}, headers=h)
    assert res.status_code == 200
    version = res.json()["version"]
    assert any(s["id"] == "api-set" for s in client.get("/api/sets").json())
    res = client.put("/api/sets/api-set", json={"set": payload, "version": "old"}, headers=h)
    assert res.status_code == 409 and "changed in the meantime" in res.json()["detail"]
    assert client.delete(f"/api/sets/api-set?version={version}", headers=h).status_code == 200
    assert client.get("/api/sets/api-set").status_code == 404


def test_saving_a_set_writes_the_file_content_to_the_log(tmp_path, caplog):
    store = SetStore(tmp_path)
    with caplog.at_level("INFO", logger="azkviz"):
        s = store.save("logged", _payload(open_=(("Kdo napsal Máj?", "Karel Hynek Mácha"),)), None, who="127.0.0.1")
        store.save("logged", _payload(open_=(("Nová otázka?", "Ano"),)), s["version"], who="127.0.0.1")
    created, saved = [r.getMessage() for r in caplog.records if r.name == "azkviz.sets"]
    assert "'logged' created by 127.0.0.1" in created and "Kdo napsal Máj?" in created
    file_text = (tmp_path / "logged.json").read_text(encoding="utf-8").rstrip("\n")
    assert "'logged' saved by 127.0.0.1" in saved and saved.endswith(file_text)   # exactly what's on disk
