"""Загрузка и валидация конфигурации бота: config.yaml + секреты из .env."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from models import Mood


class ConfigError(Exception):
    """Ошибка чтения или валидации config.yaml."""


class BotConfig(BaseModel):
    """Секция ``bot``."""

    name: str = "Настроение в каналах"
    admin_ids: list[int] = Field(default_factory=list)


class MoodConfig(BaseModel):
    """Один элемент секции ``moods``."""

    emoji: str
    label: str
    prompt: str
    score_threshold: int = 7

    @field_validator("score_threshold")
    @classmethod
    def _threshold_in_range(cls, value: int) -> int:
        """Проверяет порог на диапазон 0–10."""
        if not 0 <= value <= 10:
            raise ValueError("score_threshold должен быть в диапазоне 0–10")
        return value

    @field_validator("emoji")
    @classmethod
    def _emoji_non_empty(cls, value: str) -> str:
        """Проверяет, что эмодзи не пустой."""
        if not value.strip():
            raise ValueError("emoji не может быть пустым")
        return value

    def to_mood(self) -> Mood:
        """Преобразует конфиг-запись в доменную модель.

        :return: объект настроения.
        """
        return Mood(
            emoji=self.emoji,
            label=self.label,
            prompt=self.prompt,
            score_threshold=self.score_threshold,
        )


class LlmConfig(BaseModel):
    """Секция ``llm``."""

    model: str
    base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    api_key_env: str = "OPENROUTER_API_KEY"
    max_tokens: int = 4000
    temperature: float = 0.2
    batch_size: int = 30
    timeout_sec: int = 90

    @field_validator("batch_size")
    @classmethod
    def _batch_positive(cls, value: int) -> int:
        """Проверяет положительность размера батча."""
        if value < 1:
            raise ValueError("llm.batch_size должен быть >= 1")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_http(cls, value: str) -> str:
        """Проверяет, что base_url похож на HTTP-адрес."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("llm.base_url должен начинаться с http:// или https://")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _api_key_env_non_empty(cls, value: str) -> str:
        """Проверяет, что имя env-переменной с ключом задано."""
        if not value.strip():
            raise ValueError("llm.api_key_env не может быть пустым")
        return value.strip()


class PeriodConfig(BaseModel):
    """Секция ``period``."""

    default_hours: int = 24
    presets: list[int] = Field(default_factory=lambda: [3, 6, 12, 24])
    min_hours: int = 1
    max_hours: int = 72

    @model_validator(mode="after")
    def _validate_ranges(self) -> PeriodConfig:
        """Проверяет согласованность диапазонов и пресетов."""
        if self.min_hours < 1:
            raise ValueError("period.min_hours должен быть >= 1")
        if self.max_hours < self.min_hours:
            raise ValueError("period.max_hours должен быть >= min_hours")
        if not self.min_hours <= self.default_hours <= self.max_hours:
            raise ValueError("period.default_hours должен попадать в [min_hours, max_hours]")
        for preset in self.presets:
            if not self.min_hours <= preset <= self.max_hours:
                raise ValueError(f"пресет {preset} вне диапазона [{self.min_hours}, {self.max_hours}]")
        return self


class DigestConfig(BaseModel):
    """Секция ``digest``."""

    top_n: int = 10
    text_limit: int = 200
    min_score_default: int = 7
    show_views: bool = True
    show_reasons: bool = True

    @field_validator("top_n")
    @classmethod
    def _top_positive(cls, value: int) -> int:
        """Проверяет положительность top_n."""
        if value < 1:
            raise ValueError("digest.top_n должен быть >= 1")
        return value


class FetchConfig(BaseModel):
    """Секция ``fetch``."""

    max_pages_per_channel: int = 3
    request_delay_sec: float = 1.0
    cache_minutes: int = 30

    @field_validator("max_pages_per_channel")
    @classmethod
    def _pages_positive(cls, value: int) -> int:
        """Проверяет положительность глубины парсинга."""
        if value < 1:
            raise ValueError("fetch.max_pages_per_channel должен быть >= 1")
        return value


class FallbackConfig(BaseModel):
    """Секция ``fallback``."""

    keywords_enabled: bool = True


class VoiceConfig(BaseModel):
    """Секция ``voice``: озвучка сводки через edge-tts."""

    enabled: bool = True
    voice: str = "ru-RU-SvetlanaNeural"
    rate: str = "+8%"
    pitch: str = "+0Hz"
    max_chars: int = 2500
    target_seconds: int = 45
    script_enabled: bool = True
    script_prompt: str = ""

    @field_validator("rate")
    @classmethod
    def _rate_format(cls, value: str) -> str:
        """Проверяет формат скорости речи, ожидаемый edge-tts."""
        if not re.fullmatch(r"[+-]\d{1,3}%", value):
            raise ValueError("voice.rate должен быть вида '+8%' или '-10%'")
        return value

    @field_validator("pitch")
    @classmethod
    def _pitch_format(cls, value: str) -> str:
        """Проверяет формат высоты голоса, ожидаемый edge-tts."""
        if not re.fullmatch(r"[+-]\d{1,3}Hz", value):
            raise ValueError("voice.pitch должен быть вида '+0Hz' или '-20Hz'")
        return value

    @field_validator("max_chars")
    @classmethod
    def _chars_in_range(cls, value: int) -> int:
        """Проверяет разумность лимита символов на озвучку."""
        if not 100 <= value <= 10_000:
            raise ValueError("voice.max_chars должен быть в диапазоне 100–10000")
        return value

    @field_validator("target_seconds")
    @classmethod
    def _seconds_positive(cls, value: int) -> int:
        """Проверяет положительность целевой длительности выпуска."""
        if value < 5:
            raise ValueError("voice.target_seconds должен быть >= 5")
        return value


class Settings(BaseModel):
    """Единый объект настроек бота, раздаётся в роутеры через Dispatcher."""

    model_config = {"arbitrary_types_allowed": True}

    bot: BotConfig
    moods: list[MoodConfig]
    llm: LlmConfig
    period: PeriodConfig
    digest: DigestConfig
    fetch: FetchConfig
    fallback: FallbackConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    texts: dict[str, str] = Field(default_factory=dict)
    bot_token: str = ""
    llm_api_key: str = ""
    config_path: Path = Field(default=Path("config.yaml"))

    @field_validator("moods")
    @classmethod
    def _unique_emojis(cls, values: list[MoodConfig]) -> list[MoodConfig]:
        """Проверяет уникальность эмодзи между настроениями."""
        seen: set[str] = set()
        for mood in values:
            if mood.emoji in seen:
                raise ValueError(f"эмодзи {mood.emoji!r} повторяется в moods")
            seen.add(mood.emoji)
        return values

    @field_validator("moods")
    @classmethod
    def _non_empty_moods(cls, values: list[MoodConfig]) -> list[MoodConfig]:
        """Проверяет, что задано хотя бы одно настроение."""
        if not values:
            raise ValueError("список moods пуст")
        return values

    def get_mood(self, emoji: str) -> Mood | None:
        """Ищет настроение по эмодзи.

        :param emoji: эмодзи-кнопка.
        :return: доменная модель настроения или None.
        """
        for mood in self.moods:
            if mood.emoji == emoji:
                return mood.to_mood()
        return None

    def get_text(self, key: str, **kwargs: Any) -> str:
        """Возвращает текст из секции texts с подстановкой плейсхолдеров.

        :param key: ключ текста.
        :param kwargs: значения для ``{placeholder}``.
        :return: итоговый текст.
        """
        template = self.texts.get(key, key)
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(f"не удалось отформатировать текст '{key}'") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    """Читает YAML-файл конфигурации.

    :param path: путь к файлу.
    :return: распарсенный словарь.
    :raises ConfigError: файл не существует или битый.
    """
    if not path.exists():
        raise ConfigError(f"файл конфигурации не найден: {path}")
    try:
        with path.open(encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml должен содержать словарь на верхнем уровне")
    return raw


def _load_secrets(api_key_env: str) -> tuple[str, str]:
    """Читает секреты из .env через python-dotenv.

    Ключ LLM берётся из env-переменной, указанной в llm.api_key_env, —
    так один и тот же код работает и с OpenRouter, и с прямым DeepSeek.

    :param api_key_env: имя env-переменной с ключом LLM.
    :return: пара (bot_token, llm_api_key).
    """
    load_dotenv()
    return (
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
        os.getenv(api_key_env, ""),
    )


def load_settings(config_path: Path = Path("config.yaml")) -> Settings:
    """Загружает и валидирует конфигурацию бота.

    :param config_path: путь к config.yaml.
    :return: готовый объект настроек.
    :raises ConfigError: конфиг невалиден.
    """
    raw = _read_yaml(config_path)
    llm_raw = raw.get("llm") or {}
    api_key_env = str(llm_raw.get("api_key_env", "OPENROUTER_API_KEY"))
    bot_token, api_key = _load_secrets(api_key_env)
    try:
        return Settings(
            **raw,
            bot_token=bot_token,
            llm_api_key=api_key,
            config_path=config_path,
        )
    except ValueError as exc:
        raise ConfigError(f"config.yaml не прошёл валидацию: {exc}") from exc
