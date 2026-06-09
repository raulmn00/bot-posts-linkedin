from typing import Protocol, runtime_checkable

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.states import PostStatus


@runtime_checkable
class PostStore(Protocol):
    """Interface de persistência de Post.

    Implementações concretas: InMemoryPostStore (dev/test), FirestorePostStore (prod).
    Async para uniformizar com o restante do app (FastAPI) e com o cliente Firestore.
    """

    async def save(self, post: Post) -> None:
        """Upsert do post pelo seu id."""
        ...

    async def get(self, post_id: str) -> Post | None:
        """Retorna o post ou None se não existir."""
        ...

    async def list_by_status(self, status: PostStatus) -> list[Post]:
        """Lista posts num dado estado — útil pra debug e jobs de cleanup."""
        ...

    async def find_active_for_chat(self, chat_id: str) -> Post | None:
        """Retorna o post não-terminal mais recente desse chat_id (ou None).

        'Não-terminal' = qualquer estado exceto PUBLISHED e REJECTED. Usado pelo
        handle_command pra detectar conflito antes de criar post novo.
        """
        ...
