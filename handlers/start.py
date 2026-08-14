"""Слой представления: /start и диспетчеризация кнопок главного меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from config import Settings
from db import Database
from handlers.channels import AddChannel, show_channels_list
from handlers.mood import send_mood_keyboard
from handlers.period import show_period_choice

router = Router(name="start")


def menu_keyboard(settings: Settings) -> ReplyKeyboardMarkup:
    """Собирает reply-клавиатуру главного меню из текстов конфига.

    :param settings: настройки бота.
    :return: клавиатура меню.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=settings.get_text("menu_channels")),
                KeyboardButton(text=settings.get_text("menu_period")),
            ],
            [
                KeyboardButton(text=settings.get_text("menu_mood")),
                KeyboardButton(text=settings.get_text("menu_my_channels")),
            ],
        ],
        resize_keyboard=True,
    )


@router.message(CommandStart())
async def on_start(message: Message, settings: Settings, db: Database) -> None:
    """Обрабатывает /start: приветствие и главное меню.

    :param message: сообщение пользователя.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    await db.ensure_user(message.from_user.id)
    await message.answer(
        settings.get_text("start"),
        reply_markup=menu_keyboard(settings),
    )


@router.message(F.text, StateFilter(None))
async def on_menu_text(
    message: Message,
    state,
    settings: Settings,
    db: Database,
) -> None:
    """Распределяет текстовые кнопки меню по действиям.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    text = message.text or ""
    if text == settings.get_text("menu_channels"):
        await state.set_state(AddChannel.waiting_username)
        await message.answer(settings.get_text("channels_prompt"))
    elif text == settings.get_text("menu_period"):
        await show_period_choice(message, state, settings, db)
    elif text == settings.get_text("menu_mood"):
        await send_mood_keyboard(message, settings)
    elif text == settings.get_text("menu_my_channels"):
        await show_channels_list(message, settings, db)
    else:
        await message.answer(settings.get_text("unknown"))
