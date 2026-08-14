"""Получение постов каналов: парсинг публичных превью t.me/s и polling getUpdates."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from config import FetchConfig
from models import Post

log = logging.getLogger(__name__)

_BASE_URL = "https://t.me"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


class ChannelUnavailableError(Exception):
    """Канал приватный или не существует."""


class FetchError(Exception):
    """Сетевая ошибка при получении данных канала."""


def normalize_username(raw: str) -> str | None:
    """Извлекает юзернейм из @username, ссылки t.me/name или голого имени.

    :param raw: строка от пользователя.
    :return: юзернейм без @ или None, если формат невалиден.
    """
    value = raw.strip().strip("/")
    if value.startswith("@"):
        value = value[1:]
    elif "t.me/" in value:
        value = value.split("t.me/", 1)[1].split("/")[0].split("?")[0]
    value = value.split("/")[0].split("?")[0]
    return value if _USERNAME_RE.match(value) else None


def _extract_text(tag: Any) -> str:
    """Достаёт текст поста из HTML-тега превью.

    :param tag: корневой тег сообщения.
    :return: текст поста, без HTML.
    """
    text_tag = tag.select_one(".tgme_widget_message_text")
    if text_tag is None:
        return ""
    return text_tag.get_text("\n", strip=True)


def _extract_views(tag: Any) -> int:
    """Достаёт число просмотров из превью.

    :param tag: корневой тег сообщения.
    :return: число просмотров, 0 если не указано.
    """
    views_tag = tag.select_one(".tgme_widget_message_views")
    if views_tag is None:
        return 0
    value = views_tag.get_text(strip=True).replace("views", "").strip()
    try:
        if value.endswith("K"):
            return int(float(value[:-1]) * 1000)
        if value.endswith("M"):
            return int(float(value[:-1]) * 1_000_000)
        return int(value.replace(" ", ""))
    except ValueError:
        return 0


def parse_preview_html(html: str, username: str) -> list[Post]:
    """Парсит одну HTML-страницу превью t.me/s.

    :param html: сырой HTML страницы.
    :param username: юзернейм канала.
    :return: список постов со страницы.
    """
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        tag = wrap.select_one(".tgme_widget_message")
        if tag is None:
            continue
        date_tag = wrap.select_one("time")
        if date_tag is None or not date_tag.get("datetime"):
            continue
        text = _extract_text(tag)
        if not text:
            continue
        post_id = wrap.get("data-post", "").split("/")[-1]
        if not post_id:
            continue
        link_tag = wrap.select_one("a.tgme_widget_message_date")
        url = link_tag.get("href", f"https://t.me/{username}/{post_id}") if link_tag else f"https://t.me/{username}/{post_id}"
        posts.append(
            Post(
                channel_username=username,
                post_id=post_id,
                text=text,
                published=date_tag["datetime"],
                views=_extract_views(tag),
                url=url,
            )
        )
    return posts


class Fetcher:
    """Клиент получения постов публичных каналов через превью t.me/s."""

    def __init__(self, config: FetchConfig, client: httpx.AsyncClient | None = None) -> None:
        """Создаёт клиент.

        :param config: секция fetch настроек.
        :param client: готовый httpx-клиент (для тестов и переиспользования).
        """
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; MoodBot/1.0)"},
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=10.0),
        )

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент, если он был создан внутри."""
        if self._owns_client:
            await self._client.aclose()

    async def check_available(self, username: str) -> None:
        """Проверяет доступность публичного превью канала.

        :param username: юзернейм канала без @.
        :raises ChannelUnavailableError: канал приватный или не существует.
        :raises FetchError: сетевые проблемы.
        """
        log.info("Проверяю доступность канала @%s", username)
        try:
            response = await self._client.get(f"{_BASE_URL}/s/{username}")
        except httpx.HTTPError as exc:
            log.warning("Сетевая ошибка при проверке @%s: %s", username, exc)
            raise FetchError(f"не удалось проверить канал: {exc}") from exc
        if response.status_code in (301, 302) or response.status_code == 404:
            log.info("Канал @%s недоступен публично (HTTP %s)", username, response.status_code)
            raise ChannelUnavailableError(username)
        if response.status_code != 200:
            log.warning("t.me вернул %s при проверке @%s", response.status_code, username)
            raise FetchError(f"t.me ответил {response.status_code}")
        log.info("Канал @%s доступен", username)

    async def fetch_posts(self, username: str, since: datetime) -> list[Post]:
        """Скачивает посты канала за период, начиная с since.

        :param username: юзернейм канала без @.
        :param since: нижняя граница времени публикации (aware datetime).
        :return: список постов, отсортированных от новых к старым.
        """
        collected: list[Post] = []
        log.info(
            "Парсинг @%s: с %s, до %s страниц",
            username,
            since.isoformat(timespec="minutes"),
            self._config.max_pages_per_channel,
        )
        for page in range(1, self._config.max_pages_per_channel + 1):
            url = f"{_BASE_URL}/s/{username}"
            if page > 1:
                url += f"?before={collected[-1].post_id}" if collected else ""
            log.info("Загружаю страницу %s канала @%s", page, username)
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                log.warning("Ошибка загрузки @%s (страница %s): %s", username, page, exc)
                raise FetchError(f"ошибка загрузки {username}: {exc}") from exc
            if response.status_code in (301, 302):
                log.info("Канал @%s стал недоступен (redirect)", username)
                raise ChannelUnavailableError(username)
            if response.status_code != 200:
                log.warning("t.me/%s ответил %s на странице %s", username, response.status_code, page)
                raise FetchError(f"t.me/{username} ответил {response.status_code}")
            page_posts = parse_preview_html(response.text, username)
            log.info(
                "Страница %s канала @%s: распарсено %s постов, свежих %s",
                page,
                username,
                len(page_posts),
                sum(1 for p in page_posts if _published_after(p.published, since)),
            )
            if not page_posts:
                log.info("Страница %s канала @%s пуста, стоп", page, username)
                break
            fresh = [p for p in page_posts if _published_after(p.published, since)]
            collected.extend(fresh)
            oldest = page_posts[-1]
            if len(fresh) < len(page_posts) or not _published_after(oldest.published, since):
                log.info("Достигнута нижняя граница окна на @%s, стоп", username)
                break
            await asyncio.sleep(self._config.request_delay_sec)
        log.info("Канал @%s: итого собрано %s постов", username, len(collected))
        return collected


def _published_after(published_iso: str, since: datetime) -> bool:
    """Проверяет, что ISO-время публикации не раньше since.

    :param published_iso: ISO-строка из тега time.
    :param since: нижняя граница.
    :return: True, если пост свежий.
    """
    try:
        published = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published >= since


async def get_updates(bot_token: str, offset: int = 0) -> list[dict[str, Any]]:
    """Получает апдейты бота через getUpdates (для каналов, где бот админ).

    :param bot_token: токен телеграм-бота.
    :param offset: смещение для длинного поллинга.
    :return: список апдейтов.
    :raises FetchError: телеграм вернул ошибку.
    """
    params: dict[str, Any] = {"timeout": 0, "allowed_updates": ["channel_post"]}
    if offset:
        params["offset"] = offset
    log.info("getUpdates: offset=%s", offset)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/getUpdates", json=params
            )
    except httpx.HTTPError as exc:
        log.warning("getUpdates не выполнился: %s", exc)
        raise FetchError(f"getUpdates не выполнился: {exc}") from exc
    if response.status_code != 200:
        log.warning("getUpdates ответил %s", response.status_code)
        raise FetchError(f"getUpdates ответил {response.status_code}")
    data = response.json()
    if not data.get("ok"):
        log.warning("getUpdates: %s", data.get("description"))
        raise FetchError(f"getUpdates: {data.get('description')}")
    log.info("getUpdates: получено %s апдейтов", len(data.get("result", [])))
    return data.get("result", [])


def update_to_post(update: dict[str, Any]) -> tuple[str, Post] | None:
    """Превращает апдейт channel_post в пару (юзернейм, пост).

    :param update: апдейт из getUpdates.
    :return: пара (username, Post) или None, если апдейт не про канал.
    """
    message = update.get("channel_post")
    if not message:
        return None
    chat = message.get("chat", {})
    username = chat.get("username")
    if not username:
        return None
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return None
    message_id = message.get("message_id")
    published = datetime.now(timezone.utc).isoformat()
    return username, Post(
        channel_username=username,
        post_id=str(message_id),
        text=text,
        published=published,
        url=f"https://t.me/{username}/{message_id}",
    )
