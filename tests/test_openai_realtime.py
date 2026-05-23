from src.services.openai_realtime import (
    OPENAI_REALTIME_PROVIDER,
    agent_uses_openai_realtime,
    build_accept_kwargs,
    accept_sip_call,
    extract_call_id,
)


class FakeCalls:
    def __init__(self):
        self.accepted = None

    def accept(self, call_id, **kwargs):
        self.accepted = (call_id, kwargs)


class FakeClient:
    def __init__(self):
        self.calls = FakeCalls()
        self.realtime = type("Realtime", (), {"calls": self.calls})()


def test_agent_uses_openai_realtime_from_voice_provider():
    assert agent_uses_openai_realtime({"voice_provider": OPENAI_REALTIME_PROVIDER}) is True
    assert agent_uses_openai_realtime({"voice_provider": "twilio_polly"}) is False


def test_agent_uses_openai_realtime_from_legacy_voice_escape_hatch():
    assert agent_uses_openai_realtime({"voice": OPENAI_REALTIME_PROVIDER}) is True


def test_build_accept_kwargs_configures_realtime_audio_session():
    kwargs = build_accept_kwargs(
        instructions="Be concise and helpful.",
        model="gpt-realtime",
        voice="marin",
    )

    assert kwargs["type"] == "realtime"
    assert kwargs["model"] == "gpt-realtime"
    assert kwargs["instructions"] == "Be concise and helpful."
    assert kwargs["output_modalities"] == ["audio"]
    assert kwargs["audio"]["output"]["voice"] == "marin"
    assert kwargs["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"


def test_accept_sip_call_invokes_openai_realtime_calls_accept():
    client = FakeClient()

    accept_sip_call(
        "call_123",
        instructions="You are Hermes on a phone call.",
        model="gpt-realtime",
        voice="cedar",
        client=client,
    )

    call_id, kwargs = client.calls.accepted
    assert call_id == "call_123"
    assert kwargs["model"] == "gpt-realtime"
    assert kwargs["audio"]["output"]["voice"] == "cedar"


def test_extract_call_id_supports_common_openai_webhook_shapes():
    assert extract_call_id({"call_id": "call_a"}) == "call_a"
    assert extract_call_id({"call": {"id": "call_b"}}) == "call_b"
    assert extract_call_id({"data": {"call_id": "call_c"}}) == "call_c"
    assert extract_call_id({"data": {"id": "call_d"}}) == "call_d"
