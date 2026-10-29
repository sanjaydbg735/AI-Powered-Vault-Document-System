from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_demo_ui_and_samples_are_served():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "Load sample documents" in page.text

        script = await client.get("/static/app.js")
        assert script.status_code == 200
        assert "DEMO_QUESTIONS" in script.text

        sample = await client.get("/static/samples/services_agreement.md")
        assert sample.status_code == 200
        assert "Master Services Agreement" in sample.text

        missing = await client.get("/static/samples/nope.txt")
        assert missing.status_code == 404
