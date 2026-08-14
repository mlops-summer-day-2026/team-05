"""Слой представления: эмодзи-кнопки настроений и запуск пайплайна сводки."""

from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Settings
from db import Database
from services import pipeline, voice
from services.digest import build_digest_text, result_keyboard
from services.pipeline import (
    NoChannelsError,
    NoPostsError,
    NoResultsError,
    PipelineError,
)
from services.voice import VoiceError

router = Router(name="mood")

_MOOD_PREFIX = "mood"
_REFRESH_PREFIX = "refresh"
_OTHER_MOOD_CALLBACK = "other_mood"
_VOICE_PREFIX = "voice"
_AUDIO_FILENAME = "digest.mp3"


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

    voice.remember(tg_id, result)
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


async def _send_audio(bot: Bot, chat_id: int, audio: bytes, caption: str) -> None:
    """Отправляет озвучку голосовым сообщением, при отказе — обычным аудио.

    edge-tts отдаёт mp3, а sendVoice по документации ждёт OGG/OPUS и ffmpeg
    для конвертации в проекте нет. Обычно Telegram mp3 принимает, но подстраховываемся.

    :param bot: бот.
    :param chat_id: id чата.
    :param audio: mp3-байты.
    :param caption: подпись под сообщением.
    """
    try:
        await bot.send_voice(
            chat_id,
            BufferedInputFile(audio, filename=_AUDIO_FILENAME),
            caption=caption,
        )
    except TelegramBadRequest:
        await bot.send_audio(
            chat_id,
            BufferedInputFile(audio, filename=_AUDIO_FILENAME),
            caption=caption,
        )


async def _resolve_result(
    bot: Bot,
    chat_id: int,
    tg_id: int,
    mood_emoji: str,
    settings: Settings,
    db: Database,
) -> pipeline.DigestResult | None:
    """Берёт последнюю сводку из памяти, а при промахе перегоняет пайплайн.

    Промах случается только после перезапуска процесса: в ``callback_data``
    сводку не положить, она живёт в памяти.

    :param bot: бот.
    :param chat_id: id чата для ответа.
    :param tg_id: telegram id пользователя.
    :param mood_emoji: эмодзи настроения.
    :param settings: настройки бота.
    :param db: доступ к БД.
    :return: результат пайплайна или None, если собрать не удалось.
    """
    cached = voice.recall(tg_id, mood_emoji)
    if cached is not None:
        return cached
    mood = settings.get_mood(mood_emoji)
    if mood is None:
        await bot.send_message(chat_id, settings.get_text("unknown"))
        return None
    status = await bot.send_message(chat_id, settings.get_text("voice_rebuilding"))
    try:
        result = await asyncio.wait_for(
            pipeline.run_pipeline(db, settings, tg_id, mood),
            timeout=settings.llm.timeout_sec * 3 + 120,
        )
    except (NoChannelsError, NoPostsError, NoResultsError):
        await bot.edit_message_text(settings.get_text("no_results"), chat_id, status.message_id)
        return None
    except TimeoutError:
        await bot.edit_message_text(
            settings.get_text("error", error="таймаут пайплайна"), chat_id, status.message_id
        )
        return None
    except PipelineError as exc:
        await bot.edit_message_text(
            settings.get_text("error", error=str(exc)), chat_id, status.message_id
        )
        return None
    voice.remember(tg_id, result)
    await bot.delete_message(chat_id, status.message_id)
    return result


@router.callback_query(F.data.startswith(f"{_VOICE_PREFIX}:"))
async def on_voice(callback: CallbackQuery, bot: Bot, settings: Settings, db: Database) -> None:
    """Обрабатывает кнопку «Озвучить» — синтезирует и шлёт голосовой выпуск.

    :param callback: колбэк кнопки.
    :param bot: бот.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    if not settings.voice.enabled:
        await callback.answer(settings.get_text("voice_disabled"), show_alert=True)
        return
    mood_emoji = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    tg_id = callback.from_user.id
    await callback.answer()

    result = await _resolve_result(bot, chat_id, tg_id, mood_emoji, settings, db)
    if result is None:
        return

    status = await bot.send_message(chat_id, settings.get_text("voice_recording"))
    await bot.send_chat_action(chat_id=chat_id, action="record_voice")
    try:
        audio, _script, script_failed = await voice.make_voice(result, settings)
    except VoiceError as exc:
        await bot.edit_message_text(
            settings.get_text("voice_error", error=str(exc)), chat_id, status.message_id
        )
        return
    await bot.delete_message(chat_id, status.message_id)

    caption = ""
    if result.mood is not None:
        caption = settings.get_text(
            "voice_caption",
            emoji=result.mood.emoji,
            label=result.mood.label,
            hours=result.hours,
        )
    if script_failed:
        caption = f"{settings.get_text('voice_script_failed')}\n{caption}".strip()
    await _send_audio(bot, chat_id, audio, caption)


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
