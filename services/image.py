"""Иллюстрация к сводке: картинка под настроение и смысл отобранных новостей.

Промпт собирается детерминированно из настроения и текстов постов — без
дополнительного вызова текстовой модели, чтобы не удлинять путь до картинки.
Ходит в свой эндпоинт со своим ключом: секция ``llm`` может указывать на
провайдера без генерации изображений (например, прямой DeepSeek).
"""

from __future__ import annotations

import base64
import binascii

import httpx

from config import Settings
from services.pipeline import DigestResult
from services.voice import clean_for_speech

_DATA_URL_MARKER = "base64,"


class ImageError(Exception):
    """Не удалось сгенерировать иллюстрацию."""


def build_prompt(result: DigestResult, settings: Settings) -> str:
    """Собирает промпт картинки из настроения и текстов отобранных новостей.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: текст промпта для модели.
    :raises ImageError: шаблон промпта пуст или не отформатировался.
    """
    config = settings.image
    if not config.prompt:
        raise ImageError("image.prompt пуст")
    if result.mood is None:
        raise ImageError("у сводки нет настроения")

    pieces = []
    for entry in result.entries[: config.max_posts]:
        text = clean_for_speech(entry.post.text).replace("\n", " ").strip()
        if text:
            pieces.append(text[: config.post_chars])
    news = "; ".join(pieces) or result.mood.prompt

    try:
        return config.prompt.format(
            label=result.mood.label,
            mood_prompt=result.mood.prompt,
            news=news,
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise ImageError(f"image.prompt не отформатировался: {exc}") from exc


def _extract_image(payload: dict) -> bytes:
    """Достаёт первую картинку из ответа модели и декодирует data-url.

    :param payload: разобранный JSON ответа.
    :return: байты картинки.
    :raises ImageError: картинки в ответе нет или она битая.
    """
    try:
        images = payload["choices"][0]["message"].get("images") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise ImageError("неожиданный формат ответа") from exc
    if not images:
        raise ImageError("модель не вернула картинку")
    try:
        url = images[0]["image_url"]["url"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ImageError("в ответе нет image_url") from exc
    if _DATA_URL_MARKER not in url:
        raise ImageError("картинка пришла не как data-url")
    try:
        # validate=True: иначе мусорные символы молча выбрасываются
        # и на выходе получается пустая «картинка»
        picture = base64.b64decode(url.split(_DATA_URL_MARKER, 1)[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("не удалось декодировать картинку") from exc
    if not picture:
        raise ImageError("картинка пустая")
    return picture


async def _request(prompt: str, settings: Settings) -> bytes:
    """Делает один запрос к генератору картинок.

    :param prompt: текст промпта.
    :param settings: настройки бота.
    :return: байты картинки.
    :raises ImageError: сервис недоступен или картинки в ответе нет.
    """
    config = settings.image
    body = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    headers = {
        "Authorization": f"Bearer {settings.image_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
            response = await client.post(config.base_url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ImageError(f"сетевая ошибка: {exc}") from exc
    if response.status_code != 200:
        raise ImageError(f"генератор картинок ответил {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageError("ответ не разобрался как JSON") from exc
    return _extract_image(payload)


async def generate(prompt: str, settings: Settings) -> bytes:
    """Генерирует картинку по промпту, повторяя при пустом ответе.

    Модель время от времени отвечает текстом вида «вот иллюстрация:» и не
    прикладывает картинку — повтор с нажимом обычно выправляет это со второго раза.

    :param prompt: текст промпта.
    :param settings: настройки бота.
    :return: байты PNG.
    :raises ImageError: попытки исчерпаны.
    """
    config = settings.image
    if not settings.image_api_key:
        raise ImageError(f"{config.api_key_env} не задан")
    nudge = "\n\nОтветь именно изображением, а не описанием."
    last: ImageError | None = None
    for attempt in range(config.retries + 1):
        try:
            return await _request(prompt if attempt == 0 else prompt + nudge, settings)
        except ImageError as exc:
            last = exc
    raise ImageError(f"попытки исчерпаны ({config.retries + 1}): {last}")


async def make_image(result: DigestResult, settings: Settings) -> bytes:
    """Готовит иллюстрацию к сводке целиком: промпт плюс генерация.

    :param result: результат пайплайна.
    :param settings: настройки бота.
    :return: байты PNG.
    :raises ImageError: собрать промпт или сгенерировать картинку не удалось.
    """
    return await generate(build_prompt(result, settings), settings)
