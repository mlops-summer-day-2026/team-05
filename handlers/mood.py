"""Слой представления: эмодзи-кнопки настроений и запуск пайплайна сводки."""

from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Settings
from db import Database
from services import pipeline
from services.digest import build_digest_text, result_keyboard
from services.pipeline import (
    NoChannelsError,
    NoPostsError,
    NoResultsError,
    PipelineError,
)

router = Router(name="mood")

_MOOD_PREFIX = "mood"
_REFRESH_PREFIX = "refresh"
_OTHER_MOOD_CALLBACK = "other_mood"


def mood_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Собирает инлайн-клавиатуру эмодзи-настроений из конфига.

    :param settings: настройки бота.
    :return: инлайн-клавиатура.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{mood.emoji} {mood.label}",
                    callback_data=f"{_MOOD_PREFIX}:{mood.emoji}",
                )
                for mood in settings.moods
            ]
        ]
    )


async def send_mood_keyboard(message: Message, settings: Settings) -> None:
    """Отправляет клавиатуру выбора настроения.

    :param message: сообщение пользователя.
    :param settings: настройки бота.
    """
    await message.answer(
        settings.get_text("choose_mood"),
        reply_markup=mood_keyboard(settings),
    )


async def _run_digest(
    bot: Bot,
    chat_id: int,
    tg_id: int,
    mood_emoji: str,
    settings: Settings,
    db: Database,
) -> None:
    """Запускает пайплайн и отвечает сводкой или текстом ошибки.

    :param bot: бот.
    :param chat_id: id чата для ответа.
    :param tg_id: telegram id пользователя.
    :param mood_emoji: эмодзи настроения.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    mood = settings.get_mood(mood_emoji)
    if mood is None:
        await bot.send_message(chat_id, settings.get_text("unknown"))
        return
    status = await bot.send_message(chat_id, "⏳")
    progress_handle = {"last": ""}

    async def progress(text: str) -> None:
        """Обновляет статусное сообщение только при изменении текста."""
        if text != progress_handle["last"]:
            await bot.edit_message_text(text, chat_id, status.message_id)
            progress_handle["last"] = text

    try:
        try:
            result = await asyncio.wait_for(
                pipeline.run_pipeline(db, settings, tg_id, mood, progress),
                timeout=settings.llm.timeout_sec * 3 + 120,
            )
        except TimeoutError as exc:
            raise PipelineError(settings.get_text("error", error="таймаут пайплайна")) from exc
    except NoChannelsError:
        await bot.edit_message_text(settings.get_text("no_channels"), chat_id, status.message_id)
        return
    except NoPostsError:
        await bot.edit_message_text(settings.get_text("no_results"), chat_id, status.message_id)
        return
    except NoResultsError:
        await bot.edit_message_text(settings.get_text("no_results"), chat_id, status.message_id)
        return
    except PipelineError as exc:
        await bot.edit_message_text(settings.get_text("error", error=str(exc)), chat_id, status.message_id)
        return

    text = build_digest_text(
        result.entries,
        result.mood,
        result.channel_names,
        result.hours,
        result.total_posts,
        settings,
    )
    if result.llm_failed:
        text = f"{settings.get_text('llm_failed')}\n\n{text}"
    await bot.edit_message_text(
        text,
        chat_id,
        status.message_id,
        reply_markup=result_keyboard(mood_emoji, settings),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(f"{_MOOD_PREFIX}:"))
async def on_mood(callback: CallbackQuery, bot: Bot, settings: Settings, db: Database) -> None:
    """Обрабатывает нажатие эмодзи-кнопки настроения.

    :param callback: колбэк кнопки.
    :param bot: бот.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    mood_emoji = callback.data.split(":", 1)[1]
    await callback.answer()
    await _run_digest(bot, callback.message.chat.id, callback.from_user.id, mood_emoji, settings, db)


@router.callback_query(F.data.startswith(f"{_REFRESH_PREFIX}:"))
async def on_refresh(callback: CallbackQuery, bot: Bot, settings: Settings, db: Database) -> None:
    """Обрабатывает кнопку «Обновить» — перезапуск пайплайна с тем же настроением.

    :param callback: колбэк кнопки.
    :param bot: бот.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    mood_emoji = callback.data.split(":", 1)[1]
    await callback.answer()
    await _run_digest(bot, callback.message.chat.id, callback.from_user.id, mood_emoji, settings, db)


@router.callback_query(F.data == _OTHER_MOOD_CALLBACK)
async def on_other_mood(callback: CallbackQuery, settings: Settings) -> None:
    """Обрабатывает кнопку «Другое настроение» — показывает эмодзи-клавиатуру.

    :param callback: колбэк кнопки.
    :param settings: настройки бота.
    """
    await callback.answer()
    await callback.message.edit_text(
        settings.get_text("choose_mood"),
        reply_markup=mood_keyboard(settings),
    )
