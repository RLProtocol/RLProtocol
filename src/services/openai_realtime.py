"""OpenAI Realtime SIP helpers.

This module is intentionally small and framework-agnostic so the telephony
webhooks can decide when to hand a call to OpenAI Realtime without coupling the
rest of ClawCall to the OpenAI SDK.
"""
from __future__ import annotations

from typing import Any, Mapping

from src.config import OPENAI_REALTIME_MODEL, OPENAI_REALTIME_VOICE

OPENAI_REALTIME_PROVIDER = "openai_realtime"
DEFAULT_TURN_DETECTION = "semantic_vad"


def agent_uses_openai_realtime(agent: Mapping[str, Any] | None) -> bool:
    """Return True when an agent should use OpenAI Realtime instead of Twilio Say.

    `voice_provider` is the new explicit setting. The legacy `voice` escape hatch
    lets operators enable the path before a migration has added the new column.
    """
    if not agent:
        return False
    return (
        (agent.get("voice_provider") or "").lower() == OPENAI_REALTIME_PROVIDER
        or (agent.get("voice") or "").lower() == OPENAI_REALTIME_PROVIDER
    )


def build_accept_kwargs(
    *,
    instructions: str,
    model: str | None = None,
    voice: str | None = None,
) -> dict[str, Any]:
    """Build kwargs for `client.realtime.calls.accept(...)`.

    The OpenAI Realtime SIP endpoint owns turn-taking and audio generation, so
    this path avoids the current STT -> webhook -> TTS loop entirely.
    """
    return {
        "type": "realtime",
        "model": model or OPENAI_REALTIME_MODEL,
        "instructions": instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "turn_detection": {"type": DEFAULT_TURN_DETECTION},
            },
            "output": {
                "voice": voice or OPENAI_REALTIME_VOICE,
            },
        },
    }


def accept_sip_call(
    call_id: str,
    *,
    instructions: str,
    model: str | None = None,
    voice: str | None = None,
    client: Any | None = None,
) -> None:
    """Accept an incoming OpenAI SIP call and bind it to a Realtime session."""
    if not call_id:
        raise ValueError("call_id is required")
    if not instructions:
        raise ValueError("instructions are required")

    if client is None:
        from openai import OpenAI

        client = OpenAI()

    client.realtime.calls.accept(
        call_id,
        **build_accept_kwargs(instructions=instructions, model=model, voice=voice),
    )


def extract_call_id(payload: Mapping[str, Any] | None) -> str | None:
    """Extract a Realtime call id from likely OpenAI webhook payload shapes."""
    if not payload:
        return None

    direct = payload.get("call_id")
    if direct:
        return str(direct)

    call = payload.get("call")
    if isinstance(call, Mapping) and call.get("id"):
        return str(call["id"])

    data = payload.get("data")
    if isinstance(data, Mapping):
        if data.get("call_id"):
            return str(data["call_id"])
        if data.get("id"):
            return str(data["id"])

    return None
