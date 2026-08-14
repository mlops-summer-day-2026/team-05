"""Слой представления: добавление, просмотр и удаление каналов."""

from __future__ import annotations

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


class AddChannel(StatesGroup):
    """FSM-состояния добавления канала."""

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


async def _add_channel(message: Message, settings: Settings, db: Database) -> str:
    """Добавляет канал: нормализует имя, проверяет доступность, сохраняет.

    :param message: сообщение пользователя.
    :param settings: настройки бота.
    :param db: доступ к БД.
    :return: текст ответа пользователю.
    """
    username = fetcher.normalize_username(message.text or "")
    if username is None:
        return settings.get_text("channels_prompt")
    if await db.get_channel(message.from_user.id, username) is not None:
        return settings.get_text("channel_exists", username=username)
    checker = fetcher.Fetcher(settings.fetch)
    mode = "public"
    try:
        try:
            await checker.check_available(username)
        except ChannelUnavailableError:
            mode = "admin_poll"
    except FetchError as exc:
        return settings.get_text("error", error=str(exc))
    finally:
        await checker.aclose()
    await db.add_channel(message.from_user.id, username, mode=mode)
    if mode == "admin_poll":
        return settings.get_text("channel_private", username=username)
    return settings.get_text("channel_added", username=username)


@router.message(AddChannel.waiting_username, F.text)
async def on_username(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """Обрабатывает ввод юзернейма канала в FSM-состоянии.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    reply = await _add_channel(message, settings, db)
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
