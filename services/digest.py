"""Форматирование сводки и клавиатур результата из конфига."""

from __future__ import annotations

from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Settings
from models import DigestEntry, Mood

_REFRESH_CALLBACK = "refresh"
_OTHER_MOOD_CALLBACK = "other_mood"


def format_views(views: int, settings: Settings) -> str:
    """Форматирует число просмотров в компактный вид.

    :param views: число просмотров.
    :param settings: настройки бота.
    :return: строка вида ``12.3K``.
    """
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M"
    if views >= 1_000:
        return settings.get_text("digest_views_k", value=f"{views / 1_000:.1f}")
    return str(views)


def format_time(published_iso: str) -> str:
    """Извлекает время HH:MM из ISO-строки публикации.

    :param published_iso: ISO-строка.
    :return: строка времени или пустая строка.
    """
    try:
        moment = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        return moment.strftime("%H:%M")
    except ValueError:
        return ""


def _truncate(text: str, limit: int) -> str:
    """Обрезает текст поста до лимита символов.

    :param text: исходный текст.
    :param limit: лимит символов.
    :return: обрезанный текст.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_digest_text(
    entries: list[DigestEntry],
    mood: Mood,
    channel_names: list[str],
    hours: int,
    total_posts: int,
    settings: Settings,
) -> str:
    """Собирает текст сводки по шаблонам из конфига.

    :param entries: отобранные посты.
    :param mood: целевое настроение.
    :param channel_names: юзернеймы каналов.
    :param hours: период в часах.
    :param total_posts: всего проанализировано постов.
    :param settings: настройки бота.
    :return: итоговый текст.
    """
    digest = settings.digest
    lines = [
        settings.get_text("digest_header", emoji=mood.emoji, label=mood.label),
        settings.get_text(
            "digest_meta",
            channels=", ".join(f"@{name}" for name in channel_names),
            hours=hours,
        ),
        settings.get_text(
            "digest_found",
            total=total_posts,
            top=min(digest.top_n, len(entries)),
        ),
        "",
    ]
    for number, entry in enumerate(entries[: digest.top_n], start=1):
        text = _truncate(entry.post.text, digest.text_limit)
        if digest.show_reasons and entry.reason:
            text = f"{text}\n💬 {entry.reason}"
        meta = f"@{entry.post.channel_username} · {format_time(entry.post.published)}"
        if digest.show_views and entry.post.views:
            meta += " · " + settings.get_text(
                "digest_views_line",
                views=format_views(entry.post.views, settings),
            )
        lines.append(
            settings.get_text(
                "digest_item",
                num=number,
                score=entry.score,
                text=text,
                meta=meta,
                url=entry.post.url,
            )
        )
        lines.append("")
    return "\n".join(lines).strip()


def result_keyboard(mood_emoji: str, settings: Settings) -> InlineKeyboardMarkup:
    """Собирает клавиатуру под сводкой: «Обновить» и «Другое настроение».

    :param mood_emoji: эмодзи текущего настроения.
    :param settings: настройки бота.
    :return: инлайн-клавиатура.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=settings.get_text("refresh"),
                    callback_data=f"{_REFRESH_CALLBACK}:{mood_emoji}",
                ),
                InlineKeyboardButton(
                    text=settings.get_text("other_mood"),
                    callback_data=_OTHER_MOOD_CALLBACK,
                ),
            ]
        ]
    )
