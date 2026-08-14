"""Слой представления: выбор периода через FSM с пресетами и пасхалкой «42»."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from db import Database

router = Router(name="period")

_SET_HOURS_PREFIX = "seth"


class SetPeriod(StatesGroup):
    """FSM-состояния выбора периода."""

    waiting_hours = State()


def _presets_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Собирает инлайн-клавиатуру пресетов периода.

    :param settings: настройки бота.
    :return: инлайн-клавиатура.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{hours} ч",
                    callback_data=f"{_SET_HOURS_PREFIX}:{hours}",
                )
                for hours in settings.period.presets
            ]
        ]
    )


async def show_period_choice(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """Показывает пресеты периода и переводит пользователя в FSM-состояние.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    current = await db.get_hours(message.from_user.id, settings.period.default_hours)
    await state.set_state(SetPeriod.waiting_hours)
    await message.answer(
        settings.get_text("choose_period", hours=current),
        reply_markup=_presets_keyboard(settings),
    )


async def _set_period(
    message: Message, state: FSMContext, settings: Settings, db: Database, value: str
) -> None:
    """Валидирует введённый период и сохраняет его.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    :param value: сырая строка с числом часов.
    """
    period = settings.period
    if value.strip() == "42":
        await state.clear()
        await message.answer(settings.get_text("meaning_of_life"))
        return
    try:
        hours = int(value.strip())
    except ValueError:
        await message.answer(
            settings.get_text("invalid_period", min=period.min_hours, max=period.max_hours)
        )
        return
    if not period.min_hours <= hours <= period.max_hours:
        await message.answer(
            settings.get_text("invalid_period", min=period.min_hours, max=period.max_hours)
        )
        return
    await db.set_hours(message.from_user.id, hours)
    await state.clear()
    await message.answer(settings.get_text("period_set", hours=hours))


@router.callback_query(F.data.startswith(f"{_SET_HOURS_PREFIX}:"))
async def on_preset(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database) -> None:
    """Обрабатывает выбор пресета периода.

    :param callback: колбэк кнопки.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    hours = int(callback.data.split(":", 1)[1])
    await db.set_hours(callback.from_user.id, hours)
    await state.clear()
    await callback.message.edit_text(settings.get_text("period_set", hours=hours))
    await callback.answer()


@router.message(SetPeriod.waiting_hours, F.text)
async def on_hours_input(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """Обрабатывает ручной ввод числа часов в FSM-состоянии.

    :param message: сообщение пользователя.
    :param state: состояние FSM.
    :param settings: настройки бота.
    :param db: доступ к БД.
    """
    await _set_period(message, state, settings, db, message.text or "")
