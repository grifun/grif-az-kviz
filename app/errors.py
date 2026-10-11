"""User-facing errors with Czech and English messages."""

from __future__ import annotations

MESSAGES = {
    "not_referee": ("Hru ovládá jen rozhodčí.", "Only the referee controls the game."),
    "unknown_action": ("Neznámá akce: {action}", "Unknown action: {action}"),
    "bad_data": ("Neplatná data.", "Invalid data."),
    "cannot_pick_now": ("Teď nelze vybírat pole.", "You can't pick a field right now."),
    "bad_cell": ("Neplatné pole.", "Invalid field."),
    "cell_taken": ("Pole už je obsazené.", "That field is already taken."),
    "no_question": ("Žádná otázka neběží.", "No question is in progress."),
    "already_open": ("Otázka už je odkrytá.", "The question is already open."),
    "need_q_and_a": ("Vyplňte otázku i odpověď.", "Fill in both the question and the answer."),
    "cancel_only_before": ("Zrušit lze jen otázku před odpovědí.", "A field can only be declined before anyone answers."),
    "nothing_to_undo": ("Není co vrátit.", "Nothing to undo."),
    "bad_player": ("Neplatný hráč.", "Invalid player."),
    "turn_only_in_pick": ("Hráče na tahu lze změnit jen při výběru pole.", "Whose turn it is can only change while picking a field."),
    "edit_only_outside": ("Pole lze upravit jen mimo otázku.", "Fields can only be edited when no question is running."),
    "bad_edit": ("Neplatná úprava pole.", "Invalid field edit."),
    "game_over_already": ("Hra už skončila.", "The game is already over."),
    "two_names": ("Zadejte dvě jména.", "Enter two names."),
    "bad_setting": ("Neplatné nastavení: {key}", "Invalid setting: {key}"),
    "bad_line": ("Chybný řádek (chybí „|“ nebo odpověď): {line}", "Bad line (missing “|” or answer): {line}"),
    "no_questions_found": ("Nenalezena žádná otázka.", "No questions found."),
    "unknown_set": ("Neznámá sada otázek: {set}", "Unknown question set: {set}"),
    "bad_set_id": ("Neplatné ID sady – použijte 1–40 malých písmen, číslic, „-“ nebo „_“.",
                   "Invalid set ID – use 1–40 lowercase letters, digits, “-” or “_”."),
    "set_exists": ("Sada s ID „{set}“ už existuje.", "A set with the ID “{set}” already exists."),
    "set_changed": ("Sada „{set}“ se mezitím změnila (v jiném okně nebo v souboru). Načtěte ji znovu a změny zopakujte.",
                    "The set “{set}” changed in the meantime (in another window or on disk). Reload it and redo your changes."),
    "set_name_missing": ("Vyplňte název sady.", "Enter a name for the set."),
    "bad_lang": ("Neplatný jazyk sady.", "Invalid set language."),
    "incomplete_question": ("Otázka č. {n} nemá vyplněnou otázku i odpověď.", "Question {n} is missing its question or answer."),
    "bad_yesno": ("Otázka ANO/NE č. {n} musí mít odpověď ANO nebo NE.", "YES/NO question {n} needs the answer YES or NO."),
    "too_many": ("Sada má příliš mnoho otázek (max. {max}).", "The set has too many questions (max {max})."),
    "edit_forbidden": ("Sady lze upravovat jen z počítače, na kterém běží server (nebo nastavte AZKVIZ_EDIT_PASSWORD).",
                       "Sets can only be edited from the computer running the server (or set AZKVIZ_EDIT_PASSWORD)."),
    "edit_password": ("Nesprávné heslo pro úpravy.", "Wrong editing password."),
    "bad_file": ("Soubor nelze načíst: {error}", "Can't load the file: {error}"),
    "game_not_found": ("Hra neexistuje (možná vypršela nebo se server restartoval).",
                       "This game doesn't exist (it may have expired or the server restarted)."),
    "bad_referee_link": ("Neplatný odkaz rozhodčího.", "Invalid referee link."),
}


class GameError(Exception):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params

    def message(self, lang: str = "cs") -> str:
        cs, en = MESSAGES.get(self.code, (self.code, self.code))
        text = en if lang == "en" else cs
        try:
            return text.format(**self.params)
        except (KeyError, IndexError):
            return text

    def __str__(self) -> str:
        return self.message("cs")
