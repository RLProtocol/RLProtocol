from flask import Flask

from src.routes import webhooks


def app():
    test_app = Flask(__name__)
    test_app.register_blueprint(webhooks.webhooks_bp)
    return test_app


def test_openai_realtime_sip_webhook_accepts_call(monkeypatch):
    accepted = {}

    def fake_db_exec(query, params=None, fetchone=False, fetchall=False):
        assert "FROM agents" in query
        return {
            "id": "agent_1",
            "name": "Hermes",
            "voice_provider": "openai_realtime",
            "realtime_model": "gpt-realtime",
            "realtime_voice": "marin",
        }

    def fake_accept_sip_call(call_id, **kwargs):
        accepted["call_id"] = call_id
        accepted.update(kwargs)

    monkeypatch.setattr(webhooks, "db_exec", fake_db_exec)
    monkeypatch.setattr(webhooks, "accept_sip_call", fake_accept_sip_call)

    response = app().test_client().post(
        "/webhooks/openai/realtime/sip?agent_id=agent_1",
        json={"call_id": "call_123"},
    )

    assert response.status_code == 204
    assert accepted["call_id"] == "call_123"
    assert accepted["model"] == "gpt-realtime"
    assert accepted["voice"] == "marin"
    assert "You are Hermes" in accepted["instructions"]


def test_openai_realtime_sip_webhook_rejects_missing_call_id(monkeypatch):
    response = app().test_client().post(
        "/webhooks/openai/realtime/sip?agent_id=agent_1",
        json={},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "call_id is required"


def test_openai_realtime_sip_webhook_get_returns_operator_status():
    response = app().test_client().get(
        "/webhooks/openai/realtime/sip?agent_id=hermes-local",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["route"] == "openai_realtime_sip"
    assert body["agent_id"] == "hermes-local"


def test_openai_realtime_sip_webhook_rejects_non_realtime_agent(monkeypatch):
    def fake_db_exec(query, params=None, fetchone=False, fetchall=False):
        return {"id": "agent_1", "name": "Hermes", "voice_provider": "twilio_polly"}

    monkeypatch.setattr(webhooks, "db_exec", fake_db_exec)

    response = app().test_client().post(
        "/webhooks/openai/realtime/sip?agent_id=agent_1",
        json={"call_id": "call_123"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "agent is not configured for OpenAI Realtime"


def test_openai_realtime_sip_webhook_supports_local_agent_without_database(monkeypatch):
    accepted = {}

    def fake_db_exec(query, params=None, fetchone=False, fetchall=False):
        raise RuntimeError("DATABASE_URL not set")

    def fake_accept_sip_call(call_id, **kwargs):
        accepted["call_id"] = call_id
        accepted.update(kwargs)

    monkeypatch.setenv("OPENAI_REALTIME_LOCAL_AGENT_ID", "hermes-local")
    monkeypatch.setenv("OPENAI_REALTIME_LOCAL_AGENT_NAME", "Hermes")
    monkeypatch.setattr(webhooks, "db_exec", fake_db_exec)
    monkeypatch.setattr(webhooks, "accept_sip_call", fake_accept_sip_call)

    response = app().test_client().post(
        "/webhooks/openai/realtime/sip?agent_id=hermes-local",
        json={"call_id": "call_123"},
    )

    assert response.status_code == 204
    assert accepted["call_id"] == "call_123"
    assert "You are Hermes" in accepted["instructions"]
