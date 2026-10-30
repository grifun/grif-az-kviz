# AZ-kvíz

A web version of the Czech TV quiz show AZ-kvíz. Each game has two windows:

- **Players' window** (`/g/<id>`): a passive projection (for a wall, projector or TV) with no controls and no cursor. It shows the board, the scores and whose turn it is. When a field is picked, its hexagon spins out of the board into a big hexagon with the answer's initials. Its outline drains as the answer clock runs. After the referee judges, it flips to the winner's colour (or black), shows the answer and flies back into the board.
- **Referee window** (`/g/<id>/referee?t=<token>`): the referee runs the whole game from here. They pick the field the player names, open the question (`O`), judge it, undo, edit fields and change settings. Keep this link private, because it shows the answers.

Clicking **Založit hru a otevřít okna** on the home page opens the players' window as a popup and turns the current tab into the referee window.

## Rules implemented

1. The first player to connect all three sides of the triangle with their own fields wins. A corner field touches two sides.
2. The player on turn names a field and the referee selects it. The players see the field and the hint. The referee then opens the question, which starts the clock. A correct answer wins the field.
3. If the answer is wrong or time runs out, the opponent chooses whether to answer. If they answer and are right, they win the field. If they are wrong too, the field turns **black**. If they pass, the field turns **black** and they pick a new field.
4. Picking a black field gives a **yes/no question**. A correct answer wins the field. A wrong answer gives it to the opponent.
5. The turn passes to the other player after every question, with one exception: if the opponent wins the field by answering in place of the player on turn, that player stays on turn. If the referee makes a mistake, **↶ O krok zpět** (top bar, `Ctrl+Z`) undoes the last step.
6. If time runs out, the player with more fields wins (**Konec času**). For a tie, the referee asks a tiebreaker and announces the winner.

**Language:** the whole app is in Czech or English. Pick the language with the CS/EN switch on the home page. The referee can change it mid-game with the CS/EN switch in their window, and both windows follow. The players' window has no controls of its own.

Field labels: numbers 1–28 for the regular round, or letters for the final. In Czech the letters are A–Ž, with "Ch" as one letter. In English they are A–Z without X, plus Ch, Sh and Th, which makes 28. In letter mode, the app prefers questions whose answer starts with the field's letter. The referee can also type in a custom question at any time.

Sound effects (a tick-tock every second, a sharper, louder tick in the last 3 s, a buzzer at zero, plus chimes for the result) play from the **referee window** by default. Browsers only allow audio in a window that has had a click, and the referee window always has. If the projector has its own speakers on a different computer, switch *Settings → Sound effects* to the players' window and click once anywhere in it. That click controls nothing.

Referee keyboard shortcuts: `O` open the question and start the clock (after a miss: the opponent answers), `S` correct, `N` wrong or time's up, `Esc` the player declines the field (after a miss: the opponent passes), `Ctrl+Z` one step back (undo). The answer time is set on the home page before the game starts.

## Questions

Each game has its own **question pool**, built from the **question sets** the referee ticks plus their own questions. You choose them on the home page before the game starts. Once the game is running, the pool can't be changed. The home page shows:

- how many questions each set has
- a **Preview** of every question in a set
- a button to select all sets in the game's language
- a warning when the pool has no regular or no YES/NO questions

### The question-set folder

Sets are loaded from the **`question_sets/`** folder, one JSON file per set. To use another folder, set the `AZKVIZ_SETS_DIR` environment variable. The folder is the source of truth:

- **Editing in the app:** open **Edit sets ↗** from the home page or the referee window, or go to `/sets` directly. You can create, rename and delete sets, and add, edit or remove questions there. There's also a search box, bulk paste in the `question | answer` format, an automatic hint preview, a `.json` download, and `Ctrl+S` to save. Each save writes that set's file at once, using a temporary file and an atomic rename.
- **Editing by hand:** the server checks the folder every 3 seconds. Files added, changed or removed in a text editor are picked up without a restart.
- **Live updates:** after a change, every open referee window updates its set list and its game's pool. A question already used in a game stays used, and a deleted set drops out of the pool.
- **Logging:** every save writes a log line to the server's standard error output, in the same stream as uvicorn's own messages. The line names the set, the client address and the file path, and it's followed by the complete content of the saved file. Deleting a set logs where its file was moved. On a host with a temporary disk, the log is your record of what was edited.
- **Deleting:** a deleted set's file is moved to `question_sets/.deleted/`, so you can bring it back by moving it out again.
- **Conflicts:** if a file changed after you opened it in the editor (by hand, or in another window), saving is refused and the editor offers to reload it. Nobody's changes get silently overwritten.
- **Broken files:** a file that isn't valid JSON, or fails validation, is skipped. The editor lists it along with the reason.

**Who may edit:** if the `AZKVIZ_EDIT_PASSWORD` environment variable is set, anyone with the password can edit. The editor asks for it once per browser tab. If it isn't set, only the computer running the server (localhost) can edit, and everyone else gets a read-only view. Set a password on any public deployment.

The sets that ship with the app:

| Language | Sets |
|---|---|
| Czech | World geography, Czechia, History and mythology, Books/music/film, Science and nature, Sport and food (157 questions), plus 50 YES/NO |
| English | Geography, Science and nature, History and culture (101 questions), plus 40 YES/NO |

A set file has `id`, `lang` (`cs`/`en`), `name` and `description` (each `{cs, en}`), `open: [{q, a, h?}]` and `yesno: [{q, a: "yes"|"no"}]`. `ANO`/`NE` also work as answers in hand-written files. The file name doesn't matter: `id` is what counts, and it falls back to the file name.

The referee's own questions (home page or *Your own questions* in the panel) are added to the pool. They can be previewed, replaced, deleted, or downloaded as `.txt` to reuse in a later game. The format is one question per line:

```
Hlavní město Francie? | Paříž
Kdo napsal Máj? | Karel Hynek Mácha | KHM      <- optional 3rd column = hint shown to players
Je Slunce hvězda? | ANO                         <- ANO/NE or YES/NO answers go to the black-field pool
```

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest
```

## Deploy (free tier)

Stack: FastAPI and WebSockets, with vanilla JS and no build step. Game state is kept **in memory**, so run **one process with one worker**.

- **Render** (free web service, supports WebSockets): push this repo to GitHub, then in Render choose *New → Blueprint* and select the repo. It uses the included `render.yaml`. The free instance goes to sleep after about 15 minutes without traffic. A running game keeps it awake (the clients ping every 25 s), but games in progress are lost if it sleeps or redeploys. The first request after a sleep takes about 30–60 s.
- **Any Docker host** (Fly.io, Koyeb, Railway, a VPS): use the included `Dockerfile`. It reads the `PORT` env variable and listens on port 8000 by default.

Games idle for 12 hours are removed. At most 500 games are kept at a time.

**Set edits on free hosting:** free tiers such as Render's have a temporary disk. Edits made in the `/sets` editor survive while the instance runs, but they are lost on a redeploy or restart, when the folder resets to what's in the repository. To keep them, download the edited sets (*Download .json*) and commit them to `question_sets/`. Alternatively, point `AZKVIZ_SETS_DIR` at a persistent disk, if your host offers one.

| Environment variable | Default | Meaning |
|---|---|---|
| `AZKVIZ_SETS_DIR` | `question_sets/` | folder with the question-set JSON files |
| `AZKVIZ_EDIT_PASSWORD` | *(unset: localhost only)* | password for the `/sets` editor |
| `PORT` | 8000 (Docker) | port to listen on |

## Layout

```
app/main.py              HTTP routes + WebSocket hub
app/game.py              board topology, win detection, turn state machine, question pool
app/questions.py         SetStore (folder of set files: load, validate, save, delete, watch), pasted questions, initials
app/errors.py            error messages in Czech and English
question_sets/*.json     the question sets (edited in /sets or by hand)
app/static/              index.html, board.html (players), referee.html, sets.html (editor), css/, js/
tests/test_game.py
```
