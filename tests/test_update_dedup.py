"""Testes do FakeUpdateDedupStore + estrutura do FirestoreUpdateDedupStore.

Semântica dedupada: mark_processed marca, already_processed retorna True após.
Firestore real é testado em prod (smoke); aqui validamos só o Protocol.
"""

import inspect

import pytest

from tests.fakes import FakeUpdateDedupStore


@pytest.mark.asyncio
async def test_fake_dedup_marca_e_detecta_update_id() -> None:
    store = FakeUpdateDedupStore()
    assert await store.already_processed(123) is False
    await store.mark_processed(123)
    assert await store.already_processed(123) is True
    # IDs diferentes são independentes
    assert await store.already_processed(456) is False


@pytest.mark.asyncio
async def test_fake_dedup_mark_calls_registra_ordem() -> None:
    store = FakeUpdateDedupStore()
    await store.mark_processed(10)
    await store.mark_processed(20)
    await store.mark_processed(30)
    assert store.mark_calls == [10, 20, 30]


def test_firestore_update_dedup_store_satisfaz_protocolo() -> None:
    from bot_posts_linkedin.services.update_dedup import FirestoreUpdateDedupStore

    assert hasattr(FirestoreUpdateDedupStore, "already_processed")
    assert hasattr(FirestoreUpdateDedupStore, "mark_processed")
    assert inspect.iscoroutinefunction(FirestoreUpdateDedupStore.already_processed)
    assert inspect.iscoroutinefunction(FirestoreUpdateDedupStore.mark_processed)
