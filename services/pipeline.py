"""Прикладной слой: пайплайн «каналы → посты → LLM → сводка».

Собирает воедино fetcher, classifier и db. Хендлеры вызывают только отсюда.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from config import Settings
from db import Database
from models import Channel, DigestEntry, Mood, Post
from services import fetcher
from services.classifier import Classifier, LLMError

ProgressCallback = Callable[[str], Awaitable[None]]
_UPDATES_OFFSET_KEY = "get_updates_offset"


class PipelineError(Exception):
    """Базовая ошибка пайплайна."""


class NoChannelsError(PipelineError):
    """У пользователя нет ни одного канала."""


class NoPostsError(PipelineError):
    """За период не нашлось ни одного поста."""


class NoResultsError(PipelineError):
    """LLM ничего не отобрал под настроение."""


@dataclass
class DigestResult:
    """Результат пайплайна для показа пользователю.

    :ivar entries: отобранные посты.
    :ivar mood: целевое настроение.
    :ivar channel_names: юзернеймы каналов.
    :ivar hours: период в часах.
    :ivar total_posts: всего проанализировано постов.
    :ivar llm_failed: True, если сработал фолбэк по ключевым словам.
    """

    entries: list[DigestEntry] = field(default_factory=list)
    mood: Mood | None = None
    channel_names: list[str] = field(default_factory=list)
    hours: int = 24
    total_posts: int = 0
    llm_failed: bool = False


def _fill_channel_meta(posts: list[Post], username: str) -> list[Post]:
    """Дополняет посты юзернеймом канала и ссылкой на оригинал.

    :param posts: посты из кеша.
    :param username: юзернейм канала.
    :return: список новых Post-объектов.
    """
    return [
        replace(
            post,
            channel_username=username,
            url=post.url or f"https://t.me/{username}/{post.post_id}",
        )
        for post in posts
    ]


async def _collect_polled(db: Database, channels: list[Channel], settings: Settings) -> None:
    """Собирает посты из getUpdates для каналов режима admin_poll.

    :param db: доступ к БД.
    :param channels: подписки пользователя.
    :param settings: настройки бота.
    :raises PipelineError: getUpdates не выполнился.
    """
    poll_channels = [c for c in channels if c.mode == "admin_poll"]
    if not poll_channels:
        return
    offset_raw = await db.get_meta(_UPDATES_OFFSET_KEY)
    offset = int(offset_raw) if offset_raw else 0
    try:
        updates = await fetcher.get_updates(settings.bot_token, offset)
    except fetcher.FetchError as exc:
        raise PipelineError(str(exc)) from exc
    if not updates:
        return
    by_username: dict[str, list[Post]] = {}
    for update in updates:
        pair = fetcher.update_to_post(update)
        if pair is None:
            continue
        username, post = pair
        by_username.setdefault(username, []).append(post)
    for channel in poll_channels:
        posts = by_username.get(channel.username)
        if posts:
            await db.save_posts(channel.id, posts)
    await db.set_meta(_UPDATES_OFFSET_KEY, max(u["update_id"] for u in updates) + 1)


def _cache_fresh(last_fetched: str | None, cache_minutes: int, now: datetime) -> bool:
    """Проверяет свежесть кеша канала.

    :param last_fetched: ISO-время последнего парсинга.
    :param cache_minutes: допустимая давность.
    :param now: текущий момент.
    :return: True, если кеш свежий.
    """
    if last_fetched is None:
        return False
    try:
        fetched_at = datetime.fromisoformat(last_fetched)
    except ValueError:
        return False
    return now - fetched_at < timedelta(minutes=cache_minutes)


async def _get_channel_posts(
    db: Database,
    channel: Channel,
    since: datetime,
    settings: Settings,
    http_fetcher: fetcher.Fetcher,
) -> list[Post]:
    """Возвращает посты одного канала за период: из кеша или свежим парсингом.

    :param db: доступ к БД.
    :param channel: канал.
    :param since: нижняя граница времени.
    :param settings: настройки бота.
    :param http_fetcher: клиент парсера.
    :return: посты с заполненным юзернеймом.
    :raises PipelineError: не удалось получить посты.
    """
    now = datetime.now(timezone.utc)
    if not _cache_fresh(await db.get_last_fetched(channel.id), settings.fetch.cache_minutes, now):
        try:
            fresh_posts = await http_fetcher.fetch_posts(channel.username, since)
        except (fetcher.FetchError, fetcher.ChannelUnavailableError) as exc:
            raise PipelineError(f"канал @{channel.username}: {exc}") from exc
        if fresh_posts:
            await db.save_posts(channel.id, fresh_posts)
        await db.set_last_fetched(channel.id, now.isoformat())
    cached = await db.get_posts_since(channel.id, since.isoformat())
    return _fill_channel_meta(cached, channel.username)


def _keyword_score(text: str, mood: Mood) -> int:
    """Оценивает пост по совпадению ключевых слов из промпта настроения.

    :param text: текст поста.
    :param mood: целевое настроение.
    :return: оценка 0–10.
    """
    keywords = {
        word.lower()
        for word in mood.prompt.replace(",", " ").split()
        if len(word) >= 4 and word.isalpha()
    }
    if not keywords:
        return 0
    lowered = text.lower()
    matches = sum(1 for word in keywords if word in lowered)
    return min(10, matches * 3)


def _fallback_filter(
    posts: list[Post], mood: Mood, top_n: int, threshold: int
) -> list[DigestEntry]:
    """Фолбэк-эвристика по ключевым словам, когда LLM недоступен.

    :param posts: посты.
    :param mood: целевое настроение.
    :param top_n: сколько брать в сводку.
    :param threshold: порог оценки.
    :return: отсортированные записи сводки.
    """
    entries = [
        DigestEntry(post=post, score=_keyword_score(post.text, mood), reason="")
        for post in posts
    ]
    entries = [e for e in entries if e.score >= threshold]
    entries.sort(key=lambda entry: entry.score, reverse=True)
    return entries[:top_n]


async def _append_posts(
    db: Database,
    channel: Channel,
    since: datetime,
    settings: Settings,
    http_fetcher: fetcher.Fetcher,
    posts: list[Post],
) -> None:
    """Получает посты канала и добавляет их в общий список.

    :param db: доступ к БД.
    :param channel: канал.
    :param since: нижняя граница времени.
    :param settings: настройки бота.
    :param http_fetcher: клиент парсера.
    :param posts: накопительный список.
    """
    posts.extend(await _get_channel_posts(db, channel, since, settings, http_fetcher))


async def run_pipeline(
    db: Database,
    settings: Settings,
    tg_id: int,
    mood: Mood,
    progress: ProgressCallback | None = None,
) -> DigestResult:
    """Запускает полный пайплайн сводки для пользователя.

    :param db: доступ к БД.
    :param settings: настройки бота.
    :param tg_id: telegram id пользователя.
    :param mood: целевое настроение.
    :param progress: опциональный колбэк сообщений о прогрессе.
    :return: результат сводки.
    :raises PipelineError: и её подклассы NoChannelsError/NoPostsError/NoResultsError.
    """
    channels = await db.list_channels(tg_id)
    if not channels:
        raise NoChannelsError("нет каналов")
    hours = await db.get_hours(tg_id, settings.period.default_hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    await _collect_polled(db, channels, settings)

    http_fetcher = fetcher.Fetcher(settings.fetch)
    posts: list[Post] = []
    try:
        await asyncio.gather(
            *[
                _append_posts(db, channel, since, settings, http_fetcher, posts)
                for channel in channels
                if channel.mode == "public"
            ]
        )
    finally:
        await http_fetcher.aclose()
    for channel in channels:
        if channel.mode == "admin_poll":
            cached = await db.get_posts_since(channel.id, since.isoformat())
            posts.extend(_fill_channel_meta(cached, channel.username))
    if not posts:
        raise NoPostsError("постов нет")
    posts.sort(key=lambda p: p.published, reverse=True)
    total = len(posts)

    if progress:
        await progress(settings.get_text("analyzing", count=total, hours=hours))

    result = DigestResult(
        mood=mood,
        channel_names=[c.username for c in channels],
        hours=hours,
        total_posts=total,
    )
    try:
        classifier = Classifier(settings.llm, settings.openrouter_api_key)
        try:
            entries = await classifier.classify(posts, mood)
        finally:
            await classifier.aclose()
    except LLMError:
        if not settings.fallback.keywords_enabled:
            raise PipelineError(settings.get_text("llm_failed_no_fallback")) from None
        threshold = mood.score_threshold or settings.digest.min_score_default
        entries = _fallback_filter(posts, mood, settings.digest.top_n, threshold)
        result.llm_failed = True
    result.entries = entries
    if not entries:
        raise NoResultsError("ничего не подошло")
    return result
