"""Cloud Tasks worker queue — substitui asyncio.create_task da Fase C.

Por que existe:
  - Em Cloud Run com scale-to-zero, asyncio.create_task pode ser cortado se o
    container hiberna após responder 200 no webhook (mesmo com --no-cpu-throttling,
    garantia não é absoluta).
  - Cloud Tasks tem retry nativo, execução durável, e desacopla webhook (rápido)
    de processamento (lento — research + geração + publicação).

Fluxo:
  1. Webhook do Telegram chama `task_queue.enqueue(action, payload)`
  2. Cloud Tasks persiste a task e responde imediato
  3. Cloud Tasks invoca POST {APP_BASE_URL}{cloud_tasks_worker_path} com OIDC token
  4. Worker valida OIDC, chama post_flow.dispatch_task(action, payload)

Em dev/teste, FakeTaskQueueClient executa síncrono — mesma semântica do antigo
asyncio.create_task, sem mudar comportamento dos testes existentes.
"""

import json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TaskQueueClient(Protocol):
    """Enfileira tasks pro worker processar.

    `action` identifica o handler (ex: "run_generation", "run_revision",
    "run_publish"). `payload` é JSON-serializable.
    """

    async def enqueue(self, action: str, payload: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class GoogleCloudTasksClient:
    """Cliente real do Cloud Tasks usando o SDK oficial async.

    Cria HTTP tasks que chamam o nosso /internal/process-task com OIDC token
    assinado pela service account configurada — o endpoint valida o token e
    rejeita 401 se inválido.
    """

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        queue: str,
        worker_url: str,
        oidc_service_account_email: str,
        oidc_audience: str | None = None,
    ) -> None:
        # Import lazy: tests com Fake não carregam o SDK (que tem deps pesadas).
        from google.cloud import tasks_v2  # noqa: PLC0415

        self._tasks_v2 = tasks_v2
        self._client = tasks_v2.CloudTasksAsyncClient()
        self._parent = self._client.queue_path(project_id, region, queue)
        self._worker_url = worker_url
        self._sa_email = oidc_service_account_email
        # Audience default = a URL completa do worker (recomendação do Google).
        self._audience = oidc_audience or worker_url

    async def enqueue(self, action: str, payload: dict[str, Any]) -> None:
        body = json.dumps({"action": action, "payload": payload}).encode("utf-8")
        task: dict[str, Any] = {
            "http_request": {
                "http_method": self._tasks_v2.HttpMethod.POST,
                "url": self._worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "oidc_token": {
                    "service_account_email": self._sa_email,
                    "audience": self._audience,
                },
            },
        }
        await self._client.create_task(parent=self._parent, task=task)

    async def close(self) -> None:
        # CloudTasksAsyncClient tem transport interno que limpa sozinho — sem
        # método close explícito, mas mantemos a interface por consistência.
        pass
