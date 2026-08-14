"""Доменные модели бота: чистые dataclasses без внешних зависимостей."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Mood:
    """Настроение из конфига: эмодзи-кнопка, подпись, промпт для LLM и порог.

    :ivar emoji: эмодзи, которым настроение выбирается в интерфейсе.
    :ivar label: человекочитаемое название.
    :ivar prompt: описание настроения для промпта LLM.
    :ivar score_threshold: минимальная оценка соответствия для попадания в сводку.
    """

    emoji: str
    label: str
    prompt: str
    score_threshold: int


@dataclass(frozen=True, slots=True)
class Channel:
    """Подписка пользователя на телеграм-канал.

    :ivar id: идентификатор записи в БД.
    :ivar username: юзернейм канала без @.
    :ivar mode: способ получения постов: ``public`` или ``admin_poll``.
    """

    id: int
    username: str
    mode: str


@dataclass(frozen=True, slots=True)
class Post:
    """Сообщение канала, кандидат на попадание в сводку.

    :ivar channel_username: юзернейм канала без @.
    :ivar post_id: уникальный идентификатор поста внутри канала.
    :ivar text: текст сообщения.
    :ivar published: ISO-строка времени публикации.
    :ivar views: количество просмотров, если известно.
    :ivar url: ссылка на оригинал.
    """

    channel_username: str
    post_id: str
    text: str
    published: str
    views: int = 0
    url: str = ""


@dataclass(frozen=True, slots=True)
class DigestEntry:
    """Отобранный пост с оценкой LLM и объяснением.

    :ivar post: сам пост.
    :ivar score: оценка соответствия настроению, 0–10.
    :ivar reason: объяснение LLM, почему пост подходит.
    """

    post: Post
    score: int
    reason: str
