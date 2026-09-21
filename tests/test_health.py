from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "event-timeline-audit"}


def test_root_is_health():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
