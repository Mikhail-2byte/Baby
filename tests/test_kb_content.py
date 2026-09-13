"""Манифест источников и оценочный набор проверяются как содержание.

Оба файла — данные, а не код, и оба должны читаться человеком без
программирования. Значит их может поправить человек без программирования, и
значит проверка обязана ловить правку, а не доверять ей.

Приём тот же, что в `test_redflags.py`: проверка живёт на импорте сборщика, а
испорченные копии подкладываются в `tmp_path`, чтобы убедиться, что проверка
вообще срабатывает. Без этой второй половины тест доказывал бы лишь, что
сегодняшний файл сегодня цел.
"""

import re

import pytest
import yaml

from kb import build_kb, eval_kb

MIN_QUESTIONS = 30


def _manifest_without(tmp_path, monkeypatch, mutate):
    """Копия sources.yaml с внесённой поломкой, подсунутая сборщику."""
    raw = yaml.safe_load(build_kb.MANIFEST.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(build_kb, "MANIFEST", path)
    return path


# --- Манифест источников ----------------------------------------------------


def test_live_manifest_passes_its_own_checks():
    tiers, sources = build_kb._load()
    assert set(tiers) == set(build_kb.TIER_KEYS)
    assert sources


def test_every_source_names_itself_and_its_licence():
    """Источник без ссылки нельзя показать, без лицензии — нельзя положить."""
    for source in build_kb.SOURCES:
        assert source.title and source.publisher and source.license, source.id
        assert source.url.startswith("http"), source.id
        assert 1990 < source.year < 2100, source.id


def test_source_ids_are_unique():
    ids = [source.id for source in build_kb.SOURCES]
    assert len(ids) == len(set(ids))


def test_a_source_is_either_fetchable_or_manual():
    """Третьего не дано: иначе сборка молча пропустила бы документ."""
    for source in build_kb.SOURCES:
        assert source.manual or source.fetch, source.id


def test_broken_tier_stops_the_build(tmp_path, monkeypatch):
    _manifest_without(tmp_path, monkeypatch, lambda raw: raw["sources"][0].update(tier="T9"))
    with pytest.raises(ValueError, match="тир"):
        build_kb._load()


def test_missing_field_stops_the_build(tmp_path, monkeypatch):
    _manifest_without(tmp_path, monkeypatch, lambda raw: raw["sources"][0].pop("license"))
    with pytest.raises(ValueError, match="license"):
        build_kb._load()


def test_duplicate_id_stops_the_build(tmp_path, monkeypatch):
    def duplicate(raw):
        raw["sources"].append(dict(raw["sources"][0]))

    _manifest_without(tmp_path, monkeypatch, duplicate)
    with pytest.raises(ValueError, match="повтор"):
        build_kb._load()


def test_source_without_fetch_or_manual_stops_the_build(tmp_path, monkeypatch):
    def strip(raw):
        raw["sources"][0].pop("fetch", None)
        raw["sources"][0].pop("manual", None)

    _manifest_without(tmp_path, monkeypatch, strip)
    with pytest.raises(ValueError, match="manual"):
        build_kb._load()


def test_empty_manifest_stops_the_build(tmp_path, monkeypatch):
    _manifest_without(tmp_path, monkeypatch, lambda raw: raw.update(sources=[]))
    with pytest.raises(ValueError, match="источник"):
        build_kb._load()


# --- Оценочный набор --------------------------------------------------------


def test_evaluation_set_is_big_enough():
    """Тридцать — не ритуал. На десяти вопросах одна случайность — это 10%."""
    questions, silence = eval_kb.load()
    assert len(questions) + len(silence) >= MIN_QUESTIONS


def test_every_question_names_an_existing_source():
    """Опечатка в id иначе означала бы вечный провал, который нечем объяснить."""
    known = {source.id for source in build_kb.SOURCES}
    questions, _ = eval_kb.load()
    for question in questions:
        assert question.source in known, question.text


def test_every_question_says_what_counts_as_an_answer():
    """Вопрос без `contains` засчитал бы любой фрагмент нужного документа."""
    questions, _ = eval_kb.load()
    for question in questions:
        assert question.contains, question.text


def test_expectations_are_lowercase_like_the_normalized_text():
    """Сравнение идёт по нормализованному тексту, а он в нижнем регистре.

    Заглавная буква в ожидании — молчаливый провал: выражение не совпадёт
    никогда, и вопрос будет вечно числиться ненайденным.
    """
    questions, _ = eval_kb.load()
    for question in questions:
        for pattern in question.contains:
            assert pattern.pattern == pattern.pattern.lower(), question.text
            assert "ё" not in pattern.pattern, question.text


def test_expectations_are_valid_regular_expressions():
    questions, _ = eval_kb.load()
    for question in questions:
        for pattern in question.contains:
            assert isinstance(pattern, re.Pattern)


def test_silence_questions_are_off_topic():
    """Раздел молчания — половина качества, и он не должен быть пустым."""
    _, silence = eval_kb.load()
    assert len(silence) >= 3


def test_every_source_is_asked_about():
    """Источник, о котором не спросили, попал в базу непроверенным.

    Исключение — те, что кладутся руками: у владельца файла может не быть.
    """
    asked = {question.source for question in eval_kb.load()[0]}
    for source in build_kb.SOURCES:
        if source.manual:
            continue
        assert source.id in asked, source.id
