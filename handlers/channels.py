"""Слой представления: добавление (в т.ч. списком), просмотр и удаление каналов."""

from __future__ import annotations

import asyncio
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from db import Database
from services import fetcher
from services.fetcher import ChannelUnavailableError, FetchError

router = Router(name="channels")

_DELETE_PREFIX = "delch"
_TOKEN_RE = re.compile(r"[\s,;]+")


class AddChannel(StatesGroup):
    """FSM-состояния добавления каналов."""

    waiting_username = State()


def _channels_keyboard(channels, settings: Settings) -> InlineKeyboardMarkup:
    """Собирает инлайн-клавиатуру списка каналов с кнопками удаления.

    :param channels: список каналов пользователя.
    :param settings: настройки бота.
    :return: инлайн-клавиатура.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"@{channel.username} ({_mode_label(channel.mode, settings)})",
                    callback_data="noop",
                ),
                InlineKeyboardButton(
                    text=settings.get_text("channel_delete"),
                    callback_data=f"{_DELETE_PREFIX}:{channel.id}",
                ),
            ]
            for channel in channels
        ]
    )


def _mode_label(mode: str, settings: Settings) -> str:
    """Возвращает человекочитаемую подпись режима канала.

    :param mode: режим из БД.
    :param settings: настройки бота.
    :return: подпись.
    """
    key = "channel_mode_poll" if mode == "admin_poll" else "channel_mode_public"
    return settings.get_text(key)


async def show_channels_list(message: Message, settings: Settings, db: Database) -> None:
    """Показывает список каналов пользователя с кнопками удаления.

    :param message: сообщение пользователя.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    channels = await db.list_channels(message.from_user.id)
    if not channels:
        await message.answer(settings.get_text("channels_empty"))
        return
    await message.answer(
        settings.get_text("channel_list"),
        reply_markup=_channels_keyboard(channels, settings),
    )


@router.callback_query(F.data.startswith(f"{_DELETE_PREFIX}:"))
async def on_delete_channel(callback: CallbackQuery, settings: Settings, db: Database) -> None:
    """Удаляет канал по инлайн-кнопке.

    :param callback: колбэк кнопки.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    channel_id = int(callback.data.split(":", 1)[1])
    username = await db.delete_channel(channel_id, callback.from_user.id)
    if username is None:
        await callback.answer(settings.get_text("unknown"))
        return
    await callback.answer(settings.get_text("channel_deleted", username=username))
    channels = await db.list_channels(callback.from_user.id)
    text = settings.get_text("channel_list")
    reply_markup = _channels_keyboard(channels, settings) if channels else None
    await callback.message.edit_text(text, reply_markup=reply_markup)


def _split_tokens(text: str) -> list[str]:
    """Разбивает сообщение на токены-кандидаты в юзернеймы.

    :param text: сообщение пользователя.
    :return: токены в порядке ввода, без пустышек и дублей.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _TOKEN_RE.split(text.strip()):
        token = raw.strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


async def _add_channels(message: Message, settings: Settings, db: Database) -> tuple[str, bool]:
    """Добавляет каналы списком: нормализует, проверяет доступность, сохраняет.

    :param message: сообщение пользователя.
    :param settings: настройки бота.
    :param db: доступ к БД.
    :return: пара (текст ответа, встретился ли хотя бы один валидный юзернейм).
    """
    tokens = _split_tokens(message.text or "")
    if not tokens:
        return settings.get_text("channels_prompt"), False

    lines: list[str] = []
    has_valid = False
    checker = fetcher.Fetcher(settings.fetch)
    try:
        for token in tokens:
            username = fetcher.normalize_username(token)
            if username is None:
                lines.append(settings.get_text("channel_invalid", raw=token))
                continue
            has_valid = True
            if await db.get_channel(message.from_user.id, username) is not None:
                lines.append(settings.get_text("channel_exists", username=username))
                continue
            mode = "public"
            try:
                try:
                    await checker.check_available(username)
                except ChannelUnavailableError:
                    mode = "admin_poll"
            except FetchError as exc:
                lines.append(
                    settings.get_text("channel_error", username=username, error=exc)
                )
                continue
            await db.add_channel(message.from_user.id, username, mode=mode)
            if mode == "admin_poll":
                lines.append(settings.get_text("channel_private", username=username))
            else:
                lines.append(settings.get_text("channel_added", username=username))
            await asyncio.sleep(settings.fetch.request_delay_sec)
    finally:
        await checker.aclose()

    if not has_valid:
        return "\n".join(lines) + f"\n\n{settings.get_text('channels_prompt')}", False
    text = "\n".join(lines)
    if len(lines) > 1:
        text = f"{settings.get_text('channels_added_header')}\n{text}"
    return text, True


@router.message(AddChannel.waiting_username, F.text)
async def on_username(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """Обрабатывает ввод юзернеймов каналов в FSM-состоянии.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    reply, done = await _add_channels(message, settings, db)
    if done:
        await state.clear()
    await message.answer(reply)


@router.message(Command("cancel"))
async def on_cancel_command(message: Message, state: FSMContext, settings: Settings) -> None:
    """Отменяет текущее FSM-состояние по команде /cancel.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    """
    await state.clear()
    await message.answer(settings.get_text("cancelled"))
