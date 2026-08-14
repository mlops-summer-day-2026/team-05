"""LLM-классификация постов по настроению через OpenRouter API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from config import LlmConfig
from models import DigestEntry, Mood, Post

log = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_JSON_ARRAY_RE = re.compile(
    r'\{\s*"id"\s*:\s*(\d+)[^{}]*?"score"\s*:\s*(\d+)[^{}]*?"reason"\s*:\s*"([^"]*)"\s*\}'
)

_PROMPT_TEMPLATE = (
    'Ты — классификатор постов телеграм-каналов.\n'
    'Настроение: «{label}» — {prompt}.\n'
    "Ниже список сообщений в формате `[id] | текст`. "
    "Отбери те, что соответствуют настроению.\n"
    'Верни строго JSON-массив объектов вида '
    '{{"id": <число>, "score": <число 0-10>, "reason": "<одно предложение, почему подходит>"}}.\n'
    "Не выдумывай id, которых нет в списке. Ничего кроме JSON не возвращай.\n\n"
    "{posts_block}"
)


class LLMError(Exception):
    """LLM недоступен или вернул необрабатываемый ответ."""


def _strip_markdown_fence(raw: str) -> str:
    """Снимает markdown-обёртку ```json ... ``` с ответа модели.

    :param raw: сырой текст ответа.
    :return: текст без обёртки.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _parse_scored(raw: str) -> list[dict[str, Any]]:
    """Парсит JSON-ответ модели со страховочным regex-извлечением.

    :param raw: текст ответа модели.
    :return: список словарей id/score/reason.
    :raises LLMError: ответ не удалось разобрать.
    """
    cleaned = _strip_markdown_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    matches = _JSON_ARRAY_RE.findall(cleaned)
    if not matches:
        raise LLMError(f"невалидный JSON от модели: {raw[:200]!r}")
    return [
        {"id": int(m[0]), "score": int(m[1]), "reason": m[2]}
        for m in matches
    ]


def build_prompt(posts: list[Post], mood: Mood, first_id: int = 1) -> str:
    """Собирает промпт классификации для батча постов.

    :param posts: батч постов.
    :param mood: целевое настроение.
    :param first_id: стартовый номер id в батче.
    :return: текст промпта.
    """
    lines = []
    for number, post in enumerate(posts, start=first_id):
        text = post.text.replace("\n", " ")
        if len(text) > 500:
            text = text[:500] + "…"
        lines.append(f"[{number}] | {text}")
    return _PROMPT_TEMPLATE.format(
        label=mood.label,
        prompt=mood.prompt,
        posts_block="\n".join(lines),
    )


class Classifier:
    """Клиент классификации постов через OpenRouter."""

    def __init__(self, config: LlmConfig, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        """Создаёт классификатор.

        :param config: секция llm настроек.
        :param api_key: ключ OpenRouter.
        :param client: готовый httpx-клиент (для тестов).
        """
        self._config = config
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_sec)

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент, если он был создан внутри."""
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, content: str) -> str:
        """Отправляет один запрос к OpenRouter и возвращает текст ответа.

        :param content: текст пользовательского сообщения.
        :return: ответ модели.
        :raises LLMError: LLM недоступен.
        """
        if not self._api_key:
            raise LLMError("OPENROUTER_API_KEY не задан")
        body = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        log.info(
            "Запрос к OpenRouter: модель=%s, промпт=%s символов, max_tokens=%s",
            self._config.model,
            len(content),
            self._config.max_tokens,
        )
        try:
            response = await self._client.post(_OPENROUTER_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("Сетевая ошибка OpenRouter: %s", exc)
            raise LLMError(f"сетевая ошибка OpenRouter: {exc}") from exc
        if response.status_code != 200:
            log.warning("OpenRouter ответил %s: %s", response.status_code, response.text[:300])
            raise LLMError(f"OpenRouter ответил {response.status_code}")
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            log.warning("Неожиданный формат ответа OpenRouter: %r", payload)
            raise LLMError(f"неожиданный формат ответа OpenRouter: {payload!r}") from exc
        log.info("Ответ OpenRouter получен: %s символов", len(content))
        return content

    async def _classify_batch(
        self, posts: list[Post], mood: Mood, first_id: int
    ) -> list[dict[str, Any]]:
        """Классифицирует один батч постов с одним повтором при битом JSON.

        :param posts: батч постов.
        :param mood: целевое настроение.
        :param first_id: стартовый номер id.
        :return: список оценок id/score/reason.
        :raises LLMError: LLM недоступен или дважды вернул нечитаемый ответ.
        """
        prompt = build_prompt(posts, mood, first_id)
        try:
            return _parse_scored(await self._post(prompt))
        except LLMError as exc:
            log.warning("Повторный вызов LLM из-за ошибки: %s", exc)
            retry_prompt = prompt + "\n\nВнимание: верни ТОЛЬКО JSON-массив, без пояснений."
            try:
                return _parse_scored(await self._post(retry_prompt))
            except LLMError as retry_exc:
                raise LLMError(f"модель дважды вернула невалидный JSON: {retry_exc}") from exc

    async def classify(self, posts: list[Post], mood: Mood) -> list[DigestEntry]:
        """Классифицирует все посты батчами и возвращает отобранные.

        :param posts: посты за период.
        :param mood: целевое настроение.
        :return: отсортированные по убыванию оценки записи сводки.
        :raises LLMError: LLM недоступен или ответ нечитаем.
        """
        scored: list[dict[str, Any]] = []
        batch_size = self._config.batch_size
        batches = (len(posts) + batch_size - 1) // batch_size
        log.info(
            "Классификация: %s постов, %s батчей по %s, настроение %s (порог %s)",
            len(posts),
            batches,
            batch_size,
            mood.emoji,
            mood.score_threshold,
        )
        for batch_number, start in enumerate(range(0, len(posts), batch_size), start=1):
            batch = posts[start : start + batch_size]
            log.info("Батч %s/%s: посты %s–%s", batch_number, batches, start + 1, start + len(batch))
            scored.extend(await self._classify_batch(batch, mood, start + 1))
        threshold = mood.score_threshold
        entries = []
        for item in scored:
            post_index = int(item.get("id", 0)) - 1
            if not 0 <= post_index < len(posts):
                log.warning("LLM вернул неизвестный id %s, пропускаю", item.get("id"))
                continue
            score = int(item.get("score", 0))
            if score < threshold:
                continue
            entries.append(
                DigestEntry(
                    post=posts[post_index],
                    score=score,
                    reason=str(item.get("reason", "")),
                )
            )
        entries.sort(key=lambda entry: entry.score, reverse=True)
        log.info(
            "Классификация завершена: модель оценила %s постов, прошли порог %s",
            len(scored),
            len(entries),
        )
        return entries
