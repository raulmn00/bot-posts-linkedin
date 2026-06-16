"""Testes do FakeTaskQueueClient + estrutura do GoogleCloudTasksClient.

GoogleCloudTasksClient real depende do SDK google-cloud-tasks com transport
gRPC — não dá pra mockar com httpx.MockTransport. Validamos via:
  - FakeTaskQueueClient: comportamento de dispatch síncrono (usado em tests)
  - GoogleCloudTasksClient: sanity de import + atributos esperados
"""

from typing import Any

import pytest

from tests.fakes import FakeTaskQueueClient


@pytest.mark.asyncio
async def test_fake_task_queue_executa_dispatch_quando_target_setado() -> None:
    """Quando set_dispatch_target é chamado, enqueue dispara dispatch_task na hora."""
    captured: list[dict[str, Any]] = []

    class _StubTarget:
        async def dispatch_task(self, action: str, payload: dict[str, Any]) -> None:
            captured.append({"action": action, "payload": payload})

    queue = FakeTaskQueueClient()
    queue.set_dispatch_target(_StubTarget())

    await queue.enqueue("run_generation", {"chat_id": "x", "post_id": "abc"})

    assert len(captured) == 1
    assert captured[0]["action"] == "run_generation"
    assert captured[0]["payload"] == {"chat_id": "x", "post_id": "abc"}
    # E o registro do call também ficou no Fake.
    assert len(queue.enqueue_calls) == 1


@pytest.mark.asyncio
async def test_fake_task_queue_sem_target_apenas_registra() -> None:
    """Sem set_dispatch_target, enqueue só registra — útil pra testar enqueue isolado."""
    queue = FakeTaskQueueClient()
    await queue.enqueue("run_publish", {"post_id": "y"})

    assert len(queue.enqueue_calls) == 1
    assert queue.enqueue_calls[0] == {"action": "run_publish", "payload": {"post_id": "y"}}


@pytest.mark.asyncio
async def test_fake_task_queue_propaga_erro_configurado() -> None:
    queue = FakeTaskQueueClient()
    queue.set_error(RuntimeError("queue cheia"))
    with pytest.raises(RuntimeError, match="queue cheia"):
        await queue.enqueue("any", {})


@pytest.mark.asyncio
async def test_fake_task_queue_close_idempotente() -> None:
    queue = FakeTaskQueueClient()
    await queue.close()
    await queue.close()
    assert queue.closed is True


def test_google_cloud_tasks_client_satisfaz_protocolo_sem_instanciar() -> None:
    """Sanity: estrutura da classe real bate com o Protocol esperado.

    Não instanciamos (precisaria credenciais GCP) — checamos atributos
    de classe + assinatura assíncrona de enqueue/close.
    """
    import inspect

    from bot_posts_linkedin.services.task_queue import GoogleCloudTasksClient

    assert hasattr(GoogleCloudTasksClient, "enqueue")
    assert hasattr(GoogleCloudTasksClient, "close")
    assert inspect.iscoroutinefunction(GoogleCloudTasksClient.enqueue)
    assert inspect.iscoroutinefunction(GoogleCloudTasksClient.close)
