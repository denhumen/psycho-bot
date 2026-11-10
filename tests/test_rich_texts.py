"""Розширені тексти в content/texts.json: HTML-розмітка Telegram і кнопки-посилання."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from aiogram.enums import ParseMode
from aiogram.methods import EditMessageText

from app.config import PROJECT_ROOT
from app.domain.content import ContentError, load_content, plain_text
from tests.conftest import close_ctx, make_settings, open_ctx
from tests.fakes import BotHarness, fake

CHAT = 7801


def content_with(tmp_path: Path, **texts: Any) -> Path:
    folder = tmp_path / "content"
    shutil.copytree(PROJECT_ROOT / "content", folder)
    path = folder / "texts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(texts)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder


def url_buttons(markup: Any) -> list[tuple[str, str]]:
    return [(b.text, b.url) for row in markup.inline_keyboard for b in row if b.url]


async def test_welcome_is_sent_as_html_with_link_buttons(tmp_path: Path) -> None:
    ctx = open_ctx(make_settings(tmp_path))
    harness, tg = BotHarness(ctx), fake(ctx)
    await harness.text(CHAT, "/start")
    [sent] = tg.sent(CHAT)
    rich = ctx.content.rich("welcome_ask_code")
    assert rich is not None
    assert sent.parse_mode == ParseMode.HTML and "<b>Вітаємо!</b>" in sent.text
    assert url_buttons(sent.reply_markup) == [(b.text, b.url) for b in rich.buttons]
    await close_ctx(ctx)


async def test_rich_text_keeps_code_buttons_and_escapes_values(tmp_path: Path) -> None:
    folder = content_with(
        tmp_path,
        consent={
            "text": "<b>Згода.</b> Питання: {support}",
            "buttons": [{"text": "Повний текст згоди", "url": "https://example.com/consent"}],
        },
    )
    ctx = open_ctx(make_settings(tmp_path, content_dir=folder, support_contact="<b>a&b</b>"))
    harness, tg = BotHarness(ctx), fake(ctx)
    await harness.text(CHAT, "RICH01")
    consent = tg.sent(CHAT)[-1]
    assert consent.parse_mode == ParseMode.HTML
    assert consent.text == "<b>Згода.</b> Питання: &lt;b&gt;a&amp;b&lt;/b&gt;"  # підстановка — як текст
    rows = consent.reply_markup.inline_keyboard
    assert [b.callback_data for row in rows for b in row if b.callback_data] == ["consent:yes", "consent:no"]
    assert url_buttons(consent.reply_markup) == [("Повний текст згоди", "https://example.com/consent")]

    # Після натискання: дії зникають, кнопка-посилання і форматування лишаються.
    await harness.press(CHAT, "consent:yes", message_text="Згода. Питання", message_id=1)
    edit = next(c for c in reversed(tg.calls) if isinstance(c, EditMessageText))
    assert edit.parse_mode == ParseMode.HTML and edit.text.endswith("✅ Погоджуюсь")
    await close_ctx(ctx)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"text": "<script>x</script>"}, "тег <script> не підтримується"),
        ({"text": "<b>незакритий"}, "незакритий тег <b>"),
        ({"text": '<a href="javascript:alert(1)">x</a>'}, "посилання має бути"),
        ({"text": "ok", "buttons": [{"text": "x", "url": "ftp://x"}]}, "url має починатися"),
        ({"text": "ok", "colour": "red"}, "Extra inputs"),
    ],
)
def test_invalid_rich_text_stops_startup(tmp_path: Path, value: dict[str, Any], error: str) -> None:
    folder = content_with(tmp_path, welcome_ask_code=value)
    with pytest.raises(ContentError, match=error):
        load_content(folder)


def test_buttons_and_fragments_must_stay_plain(tmp_path: Path) -> None:
    folder = content_with(tmp_path, btn_ready={"text": "<b>Готовий</b>"})
    with pytest.raises(ContentError, match="btn_ready: тут дозволено лише звичайний рядок"):
        load_content(folder)


def test_required_placeholders_checked_in_rich_text(tmp_path: Path) -> None:
    folder = content_with(tmp_path, final_message={"text": "<b>Дякуємо!</b>"})
    with pytest.raises(ContentError, match=r"final_message: бракує підстановок \['url'\]"):
        load_content(folder)


def test_plain_text_for_admin() -> None:
    assert plain_text('<b>Вітаємо!</b> Це <a href="https://x">бот</a> &amp; ще') == "Вітаємо! Це бот & ще"
