from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.states import PostStatus


class FirestorePostStore:
    """Stub do PostStore com backend Firestore.

    Implementação real fica na Fase G (deploy GCP). O stub existe agora pra:
      1) Manter a interface PostStore amarrada via type-check.
      2) Documentar a assinatura usada em produção.
    """

    def __init__(self, project_id: str, collection: str) -> None:
        self._project_id = project_id
        self._collection = collection

    async def save(self, post: Post) -> None:
        raise NotImplementedError("FirestorePostStore.save — implementar na Fase G")

    async def get(self, post_id: str) -> Post | None:
        raise NotImplementedError("FirestorePostStore.get — implementar na Fase G")

    async def list_by_status(self, status: PostStatus) -> list[Post]:
        raise NotImplementedError("FirestorePostStore.list_by_status — implementar na Fase G")

    async def find_active_for_chat(self, chat_id: str) -> Post | None:
        raise NotImplementedError("FirestorePostStore.find_active_for_chat — implementar na Fase G")
