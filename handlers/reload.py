"""Слой представления: /reload — перечитывание config.yaml на лету (только для админов)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram import Dispatcher

from config import ConfigError, Settings, load_settings

router = Router(name="reload")


@router.message(Command("reload"))
async def on_reload(message: Message, settings: Settings, dispatcher: Dispatcher) -> None:
    """Перечитывает config.yaml и подменяет Settings в Dispatcher.

    :param message: сообщение пользователя.
    :param settings: текущие настройки.
    :param dispatcher: диспетчер, в котором живёт Settings.
    """
    if message.from_user.id not in settings.bot.admin_ids:
        await message.answer(settings.get_text("reload_denied"))
        return
    try:
        new_settings = load_settings(settings.config_path)
    except ConfigError as exc:
        await message.answer(settings.get_text("error", error=str(exc)))
        return
    dispatcher["settings"] = new_settings
    await message.answer(new_settings.get_text("reload_ok"))
