import asyncio

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.states import PostStatus, is_terminal


class InMemoryPostStore:
    """Store em memória usado em dev/testes.

    Faz cópia defensiva via model_copy(deep=True) — assim mutações no objeto
    retornado não vazam para o storage e vice-versa, espelhando o comportamento
    que o Firestore terá em produção.
    """

    def __init__(self) -> None:
        self._data: dict[str, Post] = {}
        self._lock = asyncio.Lock()

    async def save(self, post: Post) -> None:
        async with self._lock:
            self._data[post.id] = post.model_copy(deep=True)

    async def get(self, post_id: str) -> Post | None:
        async with self._lock:
            stored = self._data.get(post_id)
            return stored.model_copy(deep=True) if stored else None

    async def list_by_status(self, status: PostStatus) -> list[Post]:
        async with self._lock:
            return [p.model_copy(deep=True) for p in self._data.values() if p.status == status]

    async def find_active_for_chat(self, chat_id: str) -> Post | None:
        async with self._lock:
            candidates = [
                p
                for p in self._data.values()
                if p.chat_id == chat_id and not is_terminal(p.status)
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda p: p.created_at)
            return latest.model_copy(deep=True)
