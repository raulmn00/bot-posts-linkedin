"""Geração e persistência de imagem.

Dois serviços ortogonais:
  - ReplicateImageService: dispara predição no Replicate, faz polling até
    succeeded/failed com timeout duro (default 60s). Retorna URL temporária.
  - GcsImageStorage: baixa a URL temporária, sobe pro nosso bucket privado
    como posts/{post_id}.png e devolve URL assinada (TTL configurável) +
    o gs:// path persistido pro Post.

O fluxo no post_flow é:
    prompt → ReplicateImageService.generate(prompt) → URL temp
                                                   → GcsImageStorage.store(URL, post_id)
                                                   → (signed_url, gs_path)

Cada um dos passos pode falhar; o post_flow trata via gather + fallback "post sem foto".
"""

import asyncio
import time
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

# -----------------------------------------------------------------------------
# Replicate
# -----------------------------------------------------------------------------


class ImageGenerationFailed(RuntimeError):
    """Replicate retornou status failed ou erro de transporte."""


class ImageTimeoutError(ImageGenerationFailed):
    """Polling estourou o REPLICATE_TIMEOUT_SECONDS."""


@runtime_checkable
class ReplicateImageService(Protocol):
    async def generate(self, prompt: str) -> str:
        """Retorna URL temporária da imagem (válida ~1h no Replicate)."""
        ...

    async def validate_credentials(self) -> None:
        """Boot fail-fast — chamada leve pra confirmar que o token autentica."""
        ...

    async def close(self) -> None: ...


class HttpxReplicateImageService:
    _BASE_URL = "https://api.replicate.com"

    def __init__(
        self,
        api_token: str,
        model: str,
        timeout_seconds: int,
        *,
        poll_interval_seconds: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # model no formato "owner/name", ex: "black-forest-labs/flux-1.1-pro".
        if "/" not in model:
            raise ValueError(f"REPLICATE_IMAGE_MODEL deve ter formato 'owner/name': {model!r}")
        self._model = model
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._client = httpx.AsyncClient(
            base_url=self._BASE_URL,
            timeout=30.0,
            headers={"Authorization": f"Bearer {api_token}"},
            transport=transport,
        )

    async def generate(self, prompt: str) -> str:
        owner, name = self._model.split("/", 1)
        r = await self._client.post(
            f"/v1/models/{owner}/{name}/predictions",
            json={"input": {"prompt": prompt}},
        )
        r.raise_for_status()
        prediction = r.json()
        prediction_id = prediction["id"]

        # Polling com deadline absoluto — mais previsível que retry count.
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_interval)
            r = await self._client.get(f"/v1/predictions/{prediction_id}")
            r.raise_for_status()
            current = r.json()
            status = current.get("status")
            if status == "succeeded":
                return _extract_output_url(current)
            if status in ("failed", "canceled"):
                err = current.get("error") or status
                raise ImageGenerationFailed(f"Replicate {status}: {err}")
            # starting / processing — continua polling

        raise ImageTimeoutError(f"Replicate excedeu {self._timeout}s")

    async def validate_credentials(self) -> None:
        r = await self._client.get("/v1/account")
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


def _extract_output_url(prediction: dict[str, Any]) -> str:
    """Flux retorna `output` como string OU lista de strings, depende da versão da API."""
    output = prediction.get("output")
    if isinstance(output, str) and output:
        return output
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str):
            return first
    raise ImageGenerationFailed(f"output inesperado do Replicate: {prediction!r}")


# -----------------------------------------------------------------------------
# GCS
# -----------------------------------------------------------------------------


@runtime_checkable
class GcsImageStorage(Protocol):
    async def store(self, image_url: str, post_id: str) -> tuple[str, str]:
        """Returns (signed_url, gs_path)."""
        ...

    async def validate_bucket(self) -> None:
        """Boot fail-fast — confirma que o bucket existe e estamos autenticados."""
        ...


# TODO Fase G — pré-requisitos no GCP:
#
# 1) Signed URL em Cloud Run sem key file requer que o service account tenha
#    `roles/iam.serviceAccountTokenCreator` SOBRE SI MESMO:
#
#      SA=bot-posts-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com
#      gcloud iam service-accounts add-iam-policy-binding $SA \
#        --member="serviceAccount:$SA" \
#        --role="roles/iam.serviceAccountTokenCreator"
#
#    Sem isso, `generate_signed_url` levanta google.auth.exceptions.RefreshError.
#
# 2) Lifecycle policy no bucket pra limpar imagens órfãs (posts REJECTED ou
#    abandonados). Ex: deletar `posts/*.png` com idade > 30 dias:
#
#      cat > lifecycle.json <<EOF
#      {"lifecycle": {"rule": [{"action": {"type": "Delete"},
#                                "condition": {"age": 30, "matchesPrefix": ["posts/"]}}]}}
#      EOF
#      gcloud storage buckets update gs://$BUCKET --lifecycle-file=lifecycle.json
class GoogleCloudStorageImpl:
    """Implementação real usando o SDK google-cloud-storage (síncrono → asyncio.to_thread)."""

    def __init__(
        self,
        bucket_name: str,
        signed_url_ttl_minutes: int,
        *,
        credentials_path: str | None = None,
        http_client_factory=None,
    ) -> None:
        # Import lazy: tests com Fake nunca pagam o custo de configurar Application
        # Default Credentials, que pode falhar/atrasar em ambientes sem credenciais.
        from google.cloud import storage  # noqa: PLC0415

        self._bucket_name = bucket_name
        self._ttl = timedelta(minutes=signed_url_ttl_minutes)
        # Por que carregar do JSON em vez de deixar o storage.Client() resolver sozinho:
        # `gcloud auth application-default login` gera end-user credentials que NÃO têm
        # private key — `blob.generate_signed_url(...)` requer chave pra assinatura.
        # O JSON da service account TEM a chave. Em prod Cloud Run, credentials_path é None
        # e o cliente cai no metadata server (que assina via IAM signBytes API).
        if credentials_path:
            self._client = storage.Client.from_service_account_json(credentials_path)
        else:
            # Cloud Run: metadata server retorna tokens SEM o scope `cloud-platform`
            # por default — e IAM signBytes API exige esse scope (ou `auth/iam`).
            # Sem isso, vira 403 "insufficient authentication scopes" mesmo com
            # o role iam.serviceAccountTokenCreator configurado.
            import google.auth  # noqa: PLC0415

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._client = storage.Client(credentials=credentials)
        # Permite override pra usar MockTransport em testes do download (futuro).
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=30.0)
        )

    async def store(self, image_url: str, post_id: str) -> tuple[str, str]:
        async with self._http_client_factory() as http:
            r = await http.get(image_url)
            r.raise_for_status()
            content = r.content

        blob_name = f"posts/{post_id}.png"
        gs_path = f"gs://{self._bucket_name}/{blob_name}"
        signed_url = await asyncio.to_thread(self._upload_and_sign, blob_name, content)
        return signed_url, gs_path

    def _upload_and_sign(self, blob_name: str, content: bytes) -> str:
        # Lazy import — só carrega google.auth.transport quando rodamos em prod real.
        import google.auth.transport.requests as gauth_req  # noqa: PLC0415

        bucket = self._client.bucket(self._bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(content, content_type="image/png")

        kwargs: dict = {
            "version": "v4",
            "expiration": self._ttl,
            "method": "GET",
        }

        # Detecta o ambiente pela credencial em uso.
        # Dev local (SA JSON via from_service_account_json): credentials TEM private_key
        # → blob.generate_signed_url assina localmente, sem chamar API.
        # Cloud Run / GCE / GKE: credentials vêm do metadata server (Compute Engine
        # Credentials) e NÃO têm private_key — só access_token OAuth. Pra assinar URL
        # precisamos passar service_account_email + access_token explícitos: o GCS SDK
        # então chama IAM signBlob API (que requer roles/iam.serviceAccountTokenCreator
        # SOBRE A PRÓPRIA SA — configurado em scripts/gcp_create_service_account.sh).
        creds = self._client._credentials
        if not getattr(creds, "private_key", None):
            # Refresh pra garantir que o access_token está fresh antes de assinar.
            creds.refresh(gauth_req.Request())
            kwargs["service_account_email"] = creds.service_account_email
            kwargs["access_token"] = creds.token

        return blob.generate_signed_url(**kwargs)

    async def validate_bucket(self) -> None:
        # `get_bucket` exige `storage.buckets.get` (vem com Storage Admin) — overkill.
        # `list_blobs(max_results=1)` exige só `storage.objects.list`, que vem com
        # objectAdmin (a role que a SA precisa pra upload+signed URL de qualquer jeito).
        await asyncio.to_thread(self._list_one_blob)

    def _list_one_blob(self) -> None:
        # list_blobs é lazy — forçar consumo pra disparar a chamada HTTP.
        list(self._client.list_blobs(self._bucket_name, max_results=1))
