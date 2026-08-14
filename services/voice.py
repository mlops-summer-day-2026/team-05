"""Озвучивание сводки: радио-скрипт через LLM и синтез речи через edge-tts.

Скрипт готовится в два эшелона, как и классификация в pipeline: сначала LLM
переписывает сводку в связный текст радиовыпуска, при сбое включается
детерминированный пересказ по шаблону. Синтез — edge-tts, бесплатный и без ключа,
на выходе mp3.
"""

from __future__ import annotations

import re
from collections import OrderedDict

import edge_tts
import httpx
from edge_tts.exceptions import EdgeTTSException

from config import Settings, VoiceConfig
from models import Mood
from services.pipeline import DigestResult

_CACHE_LIMIT = 200
_POST_CHARS_FOR_LLM = 600

_ORDINALS = (
    "первая",
    "вторая",
    "третья",
    "четвёртая",
    "пятая",
    "шестая",
    "седьмая",
    "восьмая",
    "девятая",
    "десятая",
)

_URL_RE = re.compile(r"(?:https?://|t\.me/)\S+")
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]+)")
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # эмодзи, пиктограммы, флаги
    (0x2190, 0x21FF),  # стрелки
    (0x2300, 0x27BF),  # технические знаки и дингбаты
    (0x2B00, 0x2BFF),  # дополнительные стрелки и фигуры
    (0xFE00, 0xFE0F),  # селекторы начертания
    (0x200D, 0x200D),  # zero-width joiner
    (0x20E3, 0x20E3),  # keycap
)
_EMOJI_RE = re.compile(
    "[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _EMOJI_RANGES) + "]+"
)
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]|]+")
_SPACES_RE = re.compile(r"[ \t]+")


class VoiceError(Exception):
    """Не удалось подготовить или синтезировать озвучку."""


_last_digests: OrderedDict[tuple[int, str], DigestResult] = OrderedDict()


def remember(tg_id: int, result: DigestResult) -> None:
    """Запоминает сводку пользователя для кнопки озвучки.

    В ``callback_data`` помещается только 64 байта, поэтому сводку держим
    в памяти процесса. Ключ включает настроение: у пользователя может висеть
    несколько сообщений со сводками, и каждая кнопка должна озвучить свою.

    :param tg_id: telegram id пользователя.
    :param result: результат пайплайна.
    """
    if result.mood is None:
        return
    key = (tg_id, result.mood.emoji)
    _last_digests[key] = result
    _last_digests.move_to_end(key)
    while len(_last_digests) > _CACHE_LIMIT:
        _last_digests.popitem(last=False)


def recall(tg_id: int, mood_emoji: str) -> DigestResult | None:
    """Возвращает сводку пользователя по настроению, если она ещё в памяти.

    :param tg_id: telegram id пользователя.
    :param mood_emoji: эмодзи настроения.
    :return: результат пайплайна или None после перезапуска процесса.
    """
    return _last_digests.get((tg_id, mood_emoji))


def clean_for_speech(text: str) -> str:
    """Вычищает из текста всё, что нельзя произносить вслух.

    Ссылки диктор читает посимвольно, эмодзи и markdown-разметка звучат мусором.

    :param text: исходный текст.
    :return: текст, пригодный для синтеза.
    """
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(r"\1", text)
    text = _EMOJI_RE.sub(" ", text)
    text = _MARKDOWN_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _shorten(text: str, limit: int) -> str:
    """Обрезает текст до лимита, по возможности по границе предложения.

    :param text: исходный текст.
    :param limit: максимум символов.
    :return: обрезанный текст.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if stop > limit // 2:
        return cut[: stop + 1]
    return cut.rstrip() + "…"


def _digest_block(result: DigestResult, settings: Settings) -> str:
    """Собирает список новостей для промпта радиоскрипта.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: построчный список постов.
    """
    lines = []
    for entry in result.entries[: settings.digest.top_n]:
        text = clean_for_speech(entry.post.text).replace("\n", " ")
        lines.append(f"- ({entry.post.channel_username}) {_shorten(text, _POST_CHARS_FOR_LLM)}")
    return "\n".join(lines)


def build_plain_script(result: DigestResult, settings: Settings) -> str:
    """Собирает текст озвучки без LLM: вступление и по новости на запись.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: текст для синтеза.
    """
    entries = result.entries[: settings.digest.top_n]
    label = result.mood.label.lower() if result.mood else ""
    parts = [
        settings.get_text(
            "voice_intro",
            label=label,
            hours=result.hours,
            count=len(entries),
        )
    ]
    budget = max(settings.voice.max_chars // max(len(entries), 1), 120)
    for number, entry in enumerate(entries):
        ordinal = _ORDINALS[number] if number < len(_ORDINALS) else f"номер {number + 1}"
        body = _shorten(clean_for_speech(entry.post.text).replace("\n", " "), budget)
        parts.append(f"Новость {ordinal}. Канал {entry.post.channel_username}. {body}")
    parts.append(settings.get_text("voice_outro"))
    return "\n".join(parts)


async def _request_script(digest_block: str, mood: Mood, settings: Settings) -> str:
    """Просит LLM переписать сводку в текст радиовыпуска.

    Отдельный вызов, а не ``Classifier``: тот жёстко просит ``json_object``,
    а здесь нужен обычный текст. Адрес и ключ — общие с классификацией,
    из секции ``llm``, чтобы озвучка ходила туда же, куда и отбор постов.

    :param digest_block: список новостей для промпта.
    :param mood: целевое настроение.
    :param settings: настройки бота.
    :return: сырой текст скрипта от модели.
    :raises VoiceError: LLM недоступен или ответ нечитаем.
    """
    if not settings.llm_api_key:
        raise VoiceError(f"{settings.llm.api_key_env} не задан")
    template = settings.voice.script_prompt
    if not template:
        raise VoiceError("voice.script_prompt пуст")
    try:
        prompt = template.format(
            label=mood.label,
            prompt=mood.prompt,
            seconds=settings.voice.target_seconds,
            digest_block=digest_block,
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise VoiceError(f"voice.script_prompt не отформатировался: {exc}") from exc

    body = {
        "model": settings.llm.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": settings.llm.temperature,
        "max_tokens": settings.llm.max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.llm.timeout_sec) as client:
            response = await client.post(settings.llm.base_url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise VoiceError(f"сетевая ошибка LLM: {exc}") from exc
    if response.status_code != 200:
        raise VoiceError(f"LLM ответил {response.status_code}")
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VoiceError("неожиданный формат ответа LLM") from exc


async def build_script(result: DigestResult, settings: Settings) -> tuple[str, bool]:
    """Готовит текст озвучки: радиоскрипт от LLM, при сбое — простой пересказ.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: пара (текст для синтеза, признак что LLM не сработал).
    """
    limit = settings.voice.max_chars
    if not settings.voice.script_enabled or result.mood is None:
        return _shorten(build_plain_script(result, settings), limit), False
    try:
        raw = await _request_script(_digest_block(result, settings), result.mood, settings)
    except VoiceError:
        return _shorten(build_plain_script(result, settings), limit), True
    script = clean_for_speech(raw)
    if not script:
        return _shorten(build_plain_script(result, settings), limit), True
    return _shorten(script, limit), False


async def synthesize(text: str, config: VoiceConfig) -> bytes:
    """Синтезирует речь через edge-tts и возвращает mp3 целиком.

    :param text: текст для озвучки.
    :param config: секция voice настроек.
    :return: mp3-байты.
    :raises VoiceError: сервис синтеза недоступен или вернул пустой поток.
    """
    if not text.strip():
        raise VoiceError("пустой текст для озвучки")
    communicate = edge_tts.Communicate(
        text,
        config.voice,
        rate=config.rate,
        pitch=config.pitch,
    )
    buffer = bytearray()
    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
    except (EdgeTTSException, OSError) as exc:
        raise VoiceError(f"edge-tts не ответил: {exc}") from exc
    if not buffer:
        raise VoiceError("edge-tts вернул пустой аудиопоток")
    return bytes(buffer)


async def make_voice(result: DigestResult, settings: Settings) -> tuple[bytes, str, bool]:
    """Готовит озвучку сводки целиком: скрипт плюс синтез.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: тройка (mp3-байты, текст скрипта, признак что LLM не сработал).
    :raises VoiceError: синтез не удался.
    """
    script, script_failed = await build_script(result, settings)
    audio = await synthesize(script, settings.voice)
    return audio, script, script_failed
