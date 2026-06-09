import pytest
from httpx import ASGITransport, AsyncClient

from bot_posts_linkedin.main import app


@pytest.mark.asyncio
async def test_healthz_retorna_ok() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["env"] in {"dev", "prod"}
