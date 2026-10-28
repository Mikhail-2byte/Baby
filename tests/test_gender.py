"""Бот не знает пола заранее, а сказать обязан так, чтобы не соврать.

Живая проверка 14.09 завела тестового ребёнка мальчиком и нашла четыре места,
где бот говорил о нём в женском роде: карточка («Родилась»), вопрос мастера
про дату — уже ПОСЛЕ того, как пол выбран кнопкой, кнопка панели «Уснула» и
весь `weeks.yaml`, где `/stage` и недельный дайджест сообщали про Тимофея, что
«она разглядывает свои руки».

Первые три были записаны в летописи как известное и не тронутое; четвёртое —
самое заметное семье — не было замечено вовсе: тестов на содержание
`weeks.yaml` хватало, а прочитать его глазами за мальчика никто не пробовал.

Сканируются строковые литералы, а не файл целиком: в комментариях и
докстроках «она» относится к панели, цитате и выдаче, и ловить их значило бы
завести сторож, который держат выключенным списком исключений.
"""

import ast
import pathlib
import re

from bot import keyboards, texts

# Слова, которые в текстах этого бота относятся только к ребёнку. Сторож
# нарочно грубый, по словам, а не по разбору: морфологический анализ здесь
# ничего бы не добавил, а исключений всего два и оба названы поимённо
FEMININE = re.compile(
    r"\b(она|сама)\b|\b[Рр]одилась\b|\b[Уу]снула\b|\bмалышка\b",
    re.IGNORECASE,
)

# Законные формы женского рода: обе не про ребёнка
ALLOWED = (
    "Панель обновляется сама",  # про панель
    "очередь в ней важнее",  # про беседу
    "малышку или малыша",  # оба варианта названы явно, и это верно
)

PY_SOURCES = ("bot/texts.py", "bot/keyboards.py")
YAML_SOURCES = ("bot/content/weeks.yaml",)


def _literals(path: pathlib.Path) -> list[str]:
    """Все строковые литералы модуля, кроме докстрок."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append(node.value)
    return found


def _offending(pieces: list[str], where: str) -> list[str]:
    guilty = []
    for piece in pieces:
        for line in piece.splitlines():
            if any(fragment in line for fragment in ALLOWED):
                continue
            if FEMININE.search(line):
                guilty.append(f"{where}: {line.strip()}")
    return guilty


def test_nothing_the_family_reads_assumes_a_girl() -> None:
    guilty: list[str] = []
    for name in PY_SOURCES:
        guilty += _offending(_literals(pathlib.Path(name)), name)
    for name in YAML_SOURCES:
        text = pathlib.Path(name).read_text(encoding="utf-8")
        guilty += _offending([text], name)

    assert not guilty, (
        "о ребёнке сказано в женском роде, а пол заранее неизвестен:\n"
        + "\n".join(guilty)
    )


def test_the_guard_can_actually_fail() -> None:
    """Сторож обязан ловить то, ради чего заведён.

    Без этой проверки он прошёл бы и с выражением, не находящим ничего
    вообще, — ровно та тавтология, на которой в этом проекте уже трижды
    попадались тесты.
    """
    assert _offending(["Она разглядывает свои руки"], "проба")
    assert _offending(["Родилась 13.07.2026"], "проба")
    assert _offending(["😴 Уснула"], "проба")
    assert not _offending(["Ребёнок разглядывает свои руки"], "проба")
    assert not _offending(["Панель обновляется сама."], "проба")


def test_the_panel_button_is_a_noun_like_the_others() -> None:
    """«Уснула» была единственной глагольной кнопкой и единственной с родом.

    Остальные — существительные: «Бутылочка», «Сцеживание», «Мокрый», «Стул»,
    «Вес». Поэтому «Сон» здесь не компромисс ради нейтральности, а возвращение
    к тому, как устроен весь ряд.
    """
    labels = [
        button.text
        for row in keyboards.panel(running_kind=None).inline_keyboard
        for button in row
    ]
    assert any("сон" in label.lower() for label in labels)
    assert not any("Уснула" in label for label in labels)


def test_the_wizard_asks_about_the_date_without_a_gender() -> None:
    assert "родилась" not in texts.BABY_ASK_BORN
    assert "родилась" not in texts.BABY_EDIT_ASK_BORN
