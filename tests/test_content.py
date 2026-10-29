import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.config import PROJECT_ROOT
from app.domain.content import ContentError, load_content
from app.main import create_app
from tests.conftest import make_settings

REAL = PROJECT_ROOT / "content"


def copy_content(tmp_path: Path) -> Path:
    target = tmp_path / "content"
    shutil.copytree(REAL, target)
    return target


def patch_json(path: Path, mutate: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_real_content_is_valid() -> None:
    content = load_content(REAL, "@support")
    assert content.instructions.text("A", 1)
    assert content.instructions.text("B", 7)
    assert [q.id for q in content.checkin.questions] == ["did_write", "mood"]
    assert "@support" in content.t("help")
    # Плейсхолдерні тексти позначені — при старті буде попередження.
    assert "texts.consent" in content.todo_markers()


def test_broken_json(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    (folder / "checkin.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(ContentError, match=r"checkin\.json — невалідний JSON"):
        load_content(folder)


def test_missing_day(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    patch_json(folder / "instructions.json", lambda d: d["B"].pop("7"))
    with pytest.raises(ContentError, match=r"instructions\.json.*група B: мають бути дні 1–7"):
        load_content(folder)


def test_empty_instruction(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    patch_json(folder / "instructions.json", lambda d: d["A"].update({"3": "   "}))
    with pytest.raises(ContentError, match=r"instructions\.json"):
        load_content(folder)


def test_missing_text_key(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    patch_json(folder / "texts.json", lambda d: d.pop("auto_reminder"))
    with pytest.raises(ContentError, match=r"texts\.json — auto_reminder"):
        load_content(folder)


def test_unknown_placeholder(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    patch_json(folder / "texts.json", lambda d: d.update({"started": "Старт {date} о {time} {group}"}))
    with pytest.raises(ContentError, match=r"started: невідомі підстановки \['group'\]"):
        load_content(folder)


def test_unknown_question_type(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    patch_json(folder / "checkin.json", lambda d: d["questions"][0].update({"type": "free_text"}))
    with pytest.raises(ContentError, match=r"checkin\.json"):
        load_content(folder)


def test_app_refuses_to_start_with_invalid_content(tmp_path: Path) -> None:
    folder = copy_content(tmp_path)
    (folder / "texts.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ContentError, match="texts.json"):
        create_app(make_settings(tmp_path, content_dir=folder), run_background=False)
