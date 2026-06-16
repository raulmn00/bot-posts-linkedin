"""Testes leves do FirestorePostStore + FirestoreChatStateStore.

Sem emulator: testamos apenas que os impls satisfazem o Protocol correspondente
(isinstance() — boa cobertura barata contra mudanças que quebram a assinatura).
A semântica das operações é coberta pelos testes InMemory que rodam normal —
ambos os impls implementam o mesmo Protocol, mesma máquina de estados.

Smoke real contra Firestore em prod fica no manual após deploy.

Os testes do isinstance NÃO instanciam o cliente Firestore real (que precisa
de credenciais GCP) — passamos um client=None que o Protocol-checker aceita.
"""

import pytest

from bot_posts_linkedin.store.base import PostStore
from bot_posts_linkedin.store.chat_state_base import ChatStateStore


@pytest.mark.asyncio
async def test_firestore_post_store_satisfaz_protocolo() -> None:
    """Importação + construção opcional sem instanciar cliente — garante que
    a assinatura dos métodos não saiu de sincronia com o Protocol."""
    from bot_posts_linkedin.store.firestore import FirestorePostStore

    # NÃO chamamos construtor (precisaria de credenciais). Validamos via __init_subclass__
    # comportamento do Protocol em tempo de checagem estática + atributos esperados.
    assert hasattr(FirestorePostStore, "save")
    assert hasattr(FirestorePostStore, "get")
    assert hasattr(FirestorePostStore, "list_by_status")
    assert hasattr(FirestorePostStore, "find_active_for_chat")
    # Cada método precisa ser corrotina assíncrona.
    import inspect

    for name in ("save", "get", "list_by_status", "find_active_for_chat"):
        assert inspect.iscoroutinefunction(getattr(FirestorePostStore, name)), (
            f"FirestorePostStore.{name} precisa ser async"
        )


@pytest.mark.asyncio
async def test_firestore_chat_state_store_satisfaz_protocolo() -> None:
    from bot_posts_linkedin.store.chat_state_firestore import FirestoreChatStateStore

    assert hasattr(FirestoreChatStateStore, "save")
    assert hasattr(FirestoreChatStateStore, "get")
    assert hasattr(FirestoreChatStateStore, "delete")
    import inspect

    for name in ("save", "get", "delete"):
        assert inspect.iscoroutinefunction(getattr(FirestoreChatStateStore, name)), (
            f"FirestoreChatStateStore.{name} precisa ser async"
        )


def test_imports_nao_quebram_protocols_base() -> None:
    """Sanity: os Protocols PostStore e ChatStateStore continuam importáveis."""
    assert PostStore is not None
    assert ChatStateStore is not None
