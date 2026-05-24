"""
Twilio webhook handlers.

Call flow (inbound):
  1. User dials ClawCall number
  2. Twilio → POST /webhooks/twilio/inbound
     → look up agent, check limits, log call, return <Gather>
  3. User speaks
  4. Twilio → POST /webhooks/twilio/gather?agent_id=...
     → fire ask_agent() in background thread
     → poll up to AGENT_RESPONSE_TIMEOUT seconds
     → if result: return <Say> + new <Gather>
     → if timeout: return filler <Say> + <Redirect> to /poll
  5. POST /webhooks/twilio/poll?agent_id=...&call_sid=...
     → check if result ready
     → if yes: return <Say> + new <Gather>
     → if no:  return filler + <Redirect> back to self
"""
import json
import os
import random
import threading
import logging
import urllib.parse

from flask import Blueprint, request, Response, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather

from src.db.client import db_exec
from src.services import bridge
from src.services.bridge import append_transcript, get_transcript, clear_transcript
from src.services.minutes import within_limit, add_seconds
from src.services.openai_realtime import (
    accept_sip_call,
    agent_uses_openai_realtime,
    extract_call_id,
)
from src.config import TWILIO_WEBHOOK_BASE_URL, FILLER_PHRASES, DEFAULT_VOICE

logger = logging.getLogger(__name__)
webhooks_bp = Blueprint("webhooks", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url(path: str, **params) -> str:
    base = f"{TWILIO_WEBHOOK_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if params:
        base += "?" + urllib.parse.urlencode(params)
    return base


def _twiml(resp: VoiceResponse) -> Response:
    return Response(str(resp), mimetype="text/xml")


def _say(node, text: str, voice: str = None):
    """Speak text using the agent's configured Polly voice."""
    node.say(text, voice=voice or DEFAULT_VOICE)


def _gather(resp: VoiceResponse, action: str, text: str, voice: str = None) -> VoiceResponse:
    """Append a <Gather> containing a <Say>."""
    g = resp.gather(
        input="speech",
        action=action,
        method="POST",
        timeout=10,
        speech_timeout="auto",
        language="en-US",
    )
    _say(g, text, voice)
    return resp


def _agent_voice(agent_id: str) -> str:
    """Return the stored Polly voice for an agent, falling back to default."""
    row = db_exec("SELECT voice FROM agents WHERE id=%s", (agent_id,), fetchone=True)
    return (row["voice"] if row and row.get("voice") else DEFAULT_VOICE)


def _respond(resp: VoiceResponse, text: str, end_call: bool,
             agent_id: str, log_id: str, voice: str = None) -> Response:
    """Build TwiML after receiving an agent reply."""
    if end_call:
        _say(resp, text, voice)
        resp.hangup()
    else:
        action = _url("/webhooks/twilio/gather", agent_id=agent_id, call_log_id=log_id)
        silence = _url("/webhooks/twilio/silence", agent_id=agent_id, call_log_id=log_id)
        _gather(resp, action, text, voice)
        resp.redirect(silence)
    return _twiml(resp)


def _push_call_event(push_url: str, event: dict):
    """POST a call event to the agent's configured webhook URL (background, best-effort)."""
    def worker():
        try:
            import requests as req
            req.post(push_url, json=event, timeout=10)
        except Exception as e:
            logger.warning(f"Webhook push to {push_url} failed: {e}")
    threading.Thread(target=worker, daemon=True).start()


def _realtime_instructions(agent: dict) -> str:
    """Build system instructions for an OpenAI Realtime phone session."""
    name = (agent.get("name") or "ClawCall agent").strip()
    webhook_url = (agent.get("webhook_url") or "").strip()
    return (
        f"You are {name}, a voice assistant handling a live phone call through ClawCall. "
        "Speak naturally, keep turns short, and let the caller interrupt you. "
        "If the caller asks for something you cannot verify, say so plainly instead of guessing. "
        "End the call politely when the task is complete. "
        f"Agent webhook URL for context: {webhook_url}"
    )


def _local_realtime_agent(agent_id: str) -> dict | None:
    """Return a local dev realtime agent when DATABASE_URL is unavailable.

    This lets operators run the PR from a laptop/tunnel for SIP smoke tests
    without access to ClawCall's production database.
    """
    local_id = os.environ.get("OPENAI_REALTIME_LOCAL_AGENT_ID", "").strip()
    if not local_id or agent_id != local_id:
        return None
    return {
        "id": local_id,
        "name": os.environ.get("OPENAI_REALTIME_LOCAL_AGENT_NAME", "Local ClawCall agent"),
        "webhook_url": os.environ.get("OPENAI_REALTIME_LOCAL_WEBHOOK_URL", ""),
        "voice_provider": "openai_realtime",
        "realtime_model": os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime"),
        "realtime_voice": os.environ.get("OPENAI_REALTIME_VOICE", "marin"),
    }


def _load_realtime_agent(agent_id: str) -> dict | None:
    try:
        return db_exec("SELECT * FROM agents WHERE id=%s", (agent_id,), fetchone=True)
    except RuntimeError as exc:
        if "DATABASE_URL" not in str(exc):
            raise
        return _local_realtime_agent(agent_id)


@webhooks_bp.route("/webhooks/openai/realtime/sip", methods=["POST", "GET"])
def openai_realtime_sip():
    """Accept an incoming OpenAI SIP call and attach a Realtime session.

    OpenAI calls this webhook for inbound SIP calls. The route looks up the
    ClawCall agent, confirms it is opted into the realtime provider, then calls
    `client.realtime.calls.accept(...)` with ClawCall-specific instructions.
    """
    payload = request.get_json(silent=True) or {}
    if not payload:
        payload = request.values.to_dict(flat=True)

    if request.method == "GET":
        return jsonify({
            "ok": True,
            "route": "openai_realtime_sip",
            "agent_id": request.args.get("agent_id"),
            "message": "Webhook is alive. SIP providers must POST a call_id here; browser GETs do not start calls.",
        })

    call_id = extract_call_id(payload) or request.values.get("call_id")
    if not call_id:
        return jsonify({"ok": False, "error": "call_id is required"}), 400

    agent_id = request.args.get("agent_id") or payload.get("agent_id")
    data = payload.get("data")
    if not agent_id and isinstance(data, dict):
        agent_id = data.get("agent_id")
    if not agent_id:
        return jsonify({"ok": False, "error": "agent_id is required"}), 400

    agent = _load_realtime_agent(str(agent_id))
    if not agent:
        return jsonify({"ok": False, "error": "agent not found"}), 404
    if not agent_uses_openai_realtime(agent):
        return jsonify({"ok": False, "error": "agent is not configured for OpenAI Realtime"}), 409

    accept_sip_call(
        str(call_id),
        instructions=_realtime_instructions(agent),
        model=agent.get("realtime_model"),
        voice=agent.get("realtime_voice"),
    )
    return ("", 204)


# ---------------------------------------------------------------------------
# Inbound call — entry point
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/inbound", methods=["POST"])
def inbound():
    """Twilio calls this when someone dials a ClawCall number."""
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")

    resp = VoiceResponse()

    phone_row = db_exec(
        "SELECT * FROM phone_numbers WHERE number=%s",
        (to_number,),
        fetchone=True,
    )
    if not phone_row:
        resp.say("Sorry, this number is not currently active. Goodbye.")
        resp.hangup()
        return _twiml(resp)

    # Shared pool numbers don't have a fixed agent_id — they route by from_number
    if not phone_row.get("is_shared_pool") and not phone_row.get("agent_id"):
        resp.say("Sorry, this number is not currently active. Goodbye.")
        resp.hangup()
        return _twiml(resp)

    # Shared pool number (free tier) — identify caller by their phone number
    if phone_row.get("is_shared_pool"):
        user = db_exec(
            "SELECT * FROM users WHERE phone_number=%s",
            (from_number,),
            fetchone=True,
        )
        if not user:
            resp.say(
                "Sorry, your number is not registered with ClawCall. "
                "Please set up your account and try again. Goodbye."
            )
            resp.hangup()
            return _twiml(resp)
        agent = db_exec(
            "SELECT * FROM agents WHERE user_id=%s ORDER BY created_at LIMIT 1",
            (str(user["id"]),),
            fetchone=True,
        )
        if not agent:
            resp.say("Sorry, no agent found for your account. Goodbye.")
            resp.hangup()
            return _twiml(resp)
        agent_id = str(agent["id"])
    else:
        # Dedicated number (Pro/Team) — route by the dialed number
        agent_id = str(phone_row["agent_id"])
        agent = db_exec("SELECT * FROM agents WHERE id=%s", (agent_id,), fetchone=True)
        if not agent:
            resp.say("Sorry, this number is not currently active. Goodbye.")
            resp.hangup()
            return _twiml(resp)
        user = db_exec("SELECT * FROM users WHERE id=%s", (agent["user_id"],), fetchone=True)
        if not user:
            resp.say("Sorry, this number is not currently active. Goodbye.")
            resp.hangup()
            return _twiml(resp)

    if not within_limit(str(user["id"])):
        resp.say(
            "You've reached your monthly call limit on ClawCall. "
            "Please upgrade your plan to continue. Goodbye."
        )
        resp.hangup()
        return _twiml(resp)

    log_id = db_exec(
        """
        INSERT INTO call_logs
          (id, agent_id, twilio_call_sid, direction, call_type, from_number, to_number, status, started_at)
        VALUES (gen_random_uuid(), %s, %s, 'inbound', 'user_initiated', %s, %s, 'in-progress', NOW())
        RETURNING id
        """,
        (agent_id, call_sid, from_number, to_number),
        fetchone=True,
    )["id"]

    # Start recording in background (non-blocking — call must connect first)
    _call_sid_for_rec = call_sid
    def _start_rec():
        try:
            from src.services.twilio_svc import start_recording
            start_recording(_call_sid_for_rec)
        except Exception as e:
            logger.warning(f"Could not start recording for {_call_sid_for_rec}: {e}")
    threading.Thread(target=_start_rec, daemon=True).start()

    voice = agent.get("voice") or DEFAULT_VOICE
    action = _url("/webhooks/twilio/gather", agent_id=agent_id, call_log_id=log_id)
    silence = _url("/webhooks/twilio/silence", agent_id=agent_id, call_log_id=log_id)

    _gather(resp, action, "Hello! How can I help you today?", voice)
    resp.redirect(silence)
    return _twiml(resp)


# ---------------------------------------------------------------------------
# Gather — user spoke, forward to agent
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/gather", methods=["POST"])
def gather():
    """Receive transcribed speech, send to agent, return response."""
    call_sid = request.values.get("CallSid", "")
    speech = (request.values.get("SpeechResult") or "").strip()
    agent_id = request.args.get("agent_id", "")
    log_id = request.args.get("call_log_id", "")

    resp = VoiceResponse()

    agent = db_exec("SELECT * FROM agents WHERE id=%s", (agent_id,), fetchone=True)
    if not agent:
        resp.say("Something went wrong. Goodbye.")
        resp.hangup()
        return _twiml(resp)

    voice = agent.get("voice") or DEFAULT_VOICE

    if not speech:
        action = _url("/webhooks/twilio/gather", agent_id=agent_id, call_log_id=log_id)
        _gather(resp, action, "I didn't catch that. Could you say it again?", voice)
        return _twiml(resp)

    append_transcript(call_sid, "user", speech)
    bridge.queue_message(agent_id, call_sid, speech)

    result, end_call, got_result = bridge.poll_result(call_sid)

    if not got_result:
        _say(resp, result, voice)
        resp.redirect(_url(
            "/webhooks/twilio/poll",
            agent_id=agent_id, call_sid=call_sid, call_log_id=log_id,
        ))
        return _twiml(resp)

    append_transcript(call_sid, "agent", result)
    return _respond(resp, result, end_call, agent_id, log_id, voice)


# ---------------------------------------------------------------------------
# Poll — check for delayed agent response
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/poll", methods=["POST", "GET"])
def poll():
    """Called by Twilio redirect while waiting for a slow agent response."""
    call_sid = request.values.get("CallSid") or request.args.get("call_sid", "")
    agent_id = request.args.get("agent_id", "")
    log_id = request.args.get("call_log_id", "")

    voice = _agent_voice(agent_id)
    result, end_call, got_result = bridge.get_result(call_sid, wait=10.0)
    resp = VoiceResponse()

    if not got_result:
        _say(resp, random.choice(FILLER_PHRASES), voice)
        resp.redirect(_url(
            "/webhooks/twilio/poll",
            agent_id=agent_id, call_sid=call_sid, call_log_id=log_id,
        ))
        return _twiml(resp)

    append_transcript(call_sid, "agent", result)
    return _respond(resp, result, end_call, agent_id, log_id, voice)


# ---------------------------------------------------------------------------
# Silence — caller didn't speak after agent response
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/silence", methods=["POST", "GET"])
def silence():
    agent_id = request.args.get("agent_id", "")
    log_id = request.args.get("call_log_id", "")
    voice = _agent_voice(agent_id)

    resp = VoiceResponse()
    action = _url("/webhooks/twilio/gather", agent_id=agent_id, call_log_id=log_id)
    _gather(resp, action, "Are you still there?", voice)
    resp.hangup()
    return _twiml(resp)


# ---------------------------------------------------------------------------
# Call Status — Twilio calls this when a call ends
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/status", methods=["POST"])
def call_status():
    call_sid = request.values.get("CallSid", "")
    call_status_val = request.values.get("CallStatus", "")
    duration = int(request.values.get("CallDuration", 0) or 0)

    log_row = db_exec(
        "SELECT * FROM call_logs WHERE twilio_call_sid=%s",
        (call_sid,),
        fetchone=True,
    )
    if not log_row:
        return ("ok", 200)

    agent_row = db_exec(
        "SELECT user_id, webhook_push_url FROM agents WHERE id=%s",
        (str(log_row["agent_id"]),),
        fetchone=True,
    )

    if call_status_val == "completed":
        turns = get_transcript(call_sid)
        transcript_json = json.dumps(turns) if turns else None
        db_exec(
            """UPDATE call_logs
               SET status='completed', ended_at=NOW(),
                   duration_seconds=%s, transcript_json=%s
               WHERE twilio_call_sid=%s""",
            (duration, transcript_json, call_sid),
        )
        if agent_row:
            add_seconds(str(agent_row["user_id"]), duration)

        # If this was a third_party call, finalise its record too
        if log_row.get("call_type") == "third_party":
            _finalise_third_party(call_sid, transcript_json)

    elif call_status_val in ("failed", "busy", "no-answer", "canceled"):
        db_exec(
            "UPDATE call_logs SET status=%s, ended_at=NOW() WHERE twilio_call_sid=%s",
            (call_status_val, call_sid),
        )
        if log_row.get("call_type") == "third_party":
            db_exec(
                "UPDATE third_party_calls SET status=%s WHERE twilio_call_sid=%s",
                (call_status_val, call_sid),
            )

    # Webhook push — Team tier only
    if agent_row and agent_row.get("webhook_push_url"):
        user_row = db_exec(
            "SELECT tier FROM users WHERE id=%s",
            (agent_row["user_id"],),
            fetchone=True,
        )
        if user_row and user_row["tier"] == "team":
            _push_call_event(agent_row["webhook_push_url"], {
                "event": "call.status",
                "call_sid": call_sid,
                "status": call_status_val,
                "duration_seconds": duration,
                "call_type": log_row.get("call_type"),
                "direction": log_row.get("direction"),
            })

    bridge.clear(call_sid)
    clear_transcript(call_sid)
    return ("ok", 200)


# ---------------------------------------------------------------------------
# Recording Status — Twilio posts here when a recording is ready
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/recording", methods=["POST"])
def recording_status():
    """Save the recording URL once Twilio finishes processing it."""
    call_sid = request.values.get("CallSid", "")
    recording_url = request.values.get("RecordingUrl", "")
    rec_status = request.values.get("RecordingStatus", "")

    if rec_status == "completed" and recording_url and call_sid:
        db_exec(
            "UPDATE call_logs SET recording_url=%s WHERE twilio_call_sid=%s",
            (recording_url + ".mp3", call_sid),
        )
        logger.info(f"Recording saved for call {call_sid}")

    return ("ok", 200)


def _finalise_third_party(call_sid: str, transcript_json: str | None):
    """Ensure third_party_calls row is marked completed and transcript saved."""
    row = db_exec(
        "SELECT * FROM third_party_calls WHERE twilio_call_sid=%s",
        (call_sid,),
        fetchone=True,
    )
    if not row:
        return

    if row["status"] not in ("completed",):
        db_exec(
            "UPDATE third_party_calls SET status='completed', completed_at=NOW(), transcript=%s WHERE id=%s",
            (transcript_json, str(row["id"])),
        )


# ---------------------------------------------------------------------------
# Outbound Callback — agent-initiated call to user
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/callback", methods=["POST", "GET"])
def callback_twiml():
    callback_id = request.args.get("callback_id", "")
    allow_followup = request.args.get("allow_followup", "1") == "1"
    call_sid = request.values.get("CallSid", "")

    resp = VoiceResponse()

    row = db_exec(
        "SELECT * FROM pending_callbacks WHERE id=%s",
        (callback_id,),
        fetchone=True,
    )
    if not row:
        resp.say("This callback is no longer available. Goodbye.")
        resp.hangup()
        return _twiml(resp)

    db_exec("UPDATE pending_callbacks SET status='answered' WHERE id=%s", (callback_id,))

    agent_id = str(row["agent_id"])
    voice = _agent_voice(agent_id)

    log_id = db_exec(
        """
        INSERT INTO call_logs
          (id, agent_id, twilio_call_sid, direction, call_type, status, started_at)
        VALUES (gen_random_uuid(), %s, %s, 'outbound', 'task_callback', 'in-progress', NOW())
        RETURNING id
        """,
        (agent_id, call_sid),
        fetchone=True,
    )["id"]

    if allow_followup:
        action = _url("/webhooks/twilio/gather", agent_id=agent_id, call_log_id=log_id)
        _gather(resp, action, row["message"], voice)
        resp.redirect(_url("/webhooks/twilio/silence", agent_id=agent_id, call_log_id=log_id))
    else:
        _say(resp, row["message"], voice)
        resp.hangup()

    return _twiml(resp)


# ---------------------------------------------------------------------------
# Scheduled Call TwiML
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/scheduled", methods=["POST", "GET"])
def scheduled_twiml():
    agent_id = request.args.get("agent_id", "")
    task_context = request.args.get("context", "")
    call_sid = request.values.get("CallSid", "")

    resp = VoiceResponse()

    agent = db_exec("SELECT * FROM agents WHERE id=%s", (agent_id,), fetchone=True)
    if not agent:
        resp.say("This scheduled call is no longer active. Goodbye.")
        resp.hangup()
        return _twiml(resp)

    voice = agent.get("voice") or DEFAULT_VOICE

    log_id = db_exec(
        """
        INSERT INTO call_logs
          (id, agent_id, twilio_call_sid, direction, call_type, status, started_at)
        VALUES (gen_random_uuid(), %s, %s, 'outbound', 'scheduled', 'in-progress', NOW())
        RETURNING id
        """,
        (agent_id, call_sid),
        fetchone=True,
    )["id"]

    bridge.queue_message(agent_id, call_sid, f"[SCHEDULED] {task_context}")
    result, end_call, got_result = bridge.poll_result(call_sid)

    if not got_result:
        _say(resp, result, voice)
        resp.redirect(_url(
            "/webhooks/twilio/poll",
            agent_id=agent_id, call_sid=call_sid, call_log_id=log_id,
        ))
    else:
        append_transcript(call_sid, "agent", result)
        _respond(resp, result, end_call, agent_id, log_id, voice)

    return _twiml(resp)


# ---------------------------------------------------------------------------
# Third Party Call TwiML — opening turn
# ---------------------------------------------------------------------------

@webhooks_bp.route("/webhooks/twilio/third-party", methods=["POST", "GET"])
def third_party_twiml():
    job_id = request.args.get("job_id", "")
    call_sid = request.values.get("CallSid", "")

    resp = VoiceResponse()

    job = db_exec("SELECT * FROM third_party_calls WHERE id=%s", (job_id,), fetchone=True)
    if not job:
        resp.hangup()
        return _twiml(resp)

    agent = db_exec("SELECT * FROM agents WHERE id=%s", (str(job["agent_id"]),), fetchone=True)
    if not agent:
        resp.hangup()
        return _twiml(resp)

    voice = agent.get("voice") or DEFAULT_VOICE

    db_exec(
        """
        INSERT INTO call_logs
          (id, agent_id, twilio_call_sid, direction, call_type, status, started_at)
        VALUES (gen_random_uuid(), %s, %s, 'outbound', 'third_party', 'in-progress', NOW())
        """,
        (str(job["agent_id"]), call_sid),
    )

    # Update third_party_calls with the actual call SID
    db_exec(
        "UPDATE third_party_calls SET twilio_call_sid=%s, status='in-progress' WHERE id=%s",
        (call_sid, job_id),
    )

    opening = f"[THIRD PARTY CALL]\nObjective: {job['objective']}\nContext: {job.get('context', '')}"
    bridge.queue_message(str(job["agent_id"]), call_sid, opening)
    result, end_call, got_result = bridge.poll_result(call_sid)

    if not got_result:
        _say(resp, result, voice)
        resp.redirect(_url(
            "/webhooks/twilio/third-party-poll",
            job_id=job_id, call_sid=call_sid,
        ))
        return _twiml(resp)

    append_transcript(call_sid, "agent", result)

    if end_call:
        _say(resp, result, voice)
        resp.hangup()
        _complete_third_party(job_id, call_sid, agent)
    else:
        g = resp.gather(
            input="speech",
            action=_url("/webhooks/twilio/third-party-gather", job_id=job_id),
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        _say(g, result, voice)
        resp.hangup()

    return _twiml(resp)


@webhooks_bp.route("/webhooks/twilio/third-party-poll", methods=["POST", "GET"])
def third_party_poll():
    job_id = request.args.get("job_id", "")
    call_sid = request.values.get("CallSid") or request.args.get("call_sid", "")

    job = db_exec("SELECT agent_id FROM third_party_calls WHERE id=%s", (job_id,), fetchone=True)
    voice = _agent_voice(str(job["agent_id"])) if job else DEFAULT_VOICE

    result, end_call, got_result = bridge.get_result(call_sid)
    resp = VoiceResponse()

    if not got_result:
        _say(resp, random.choice(FILLER_PHRASES), voice)
        resp.redirect(_url(
            "/webhooks/twilio/third-party-poll",
            job_id=job_id, call_sid=call_sid,
        ))
        return _twiml(resp)

    append_transcript(call_sid, "agent", result)

    if end_call:
        _say(resp, result, voice)
        resp.hangup()
        agent = db_exec(
            "SELECT * FROM agents WHERE id=%s",
            (str(job["agent_id"]),),
            fetchone=True,
        ) if job else None
        _complete_third_party(job_id, call_sid, agent)
    else:
        g = resp.gather(
            input="speech",
            action=_url("/webhooks/twilio/third-party-gather", job_id=job_id),
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        _say(g, result, voice)
        resp.hangup()

    return _twiml(resp)


@webhooks_bp.route("/webhooks/twilio/third-party-gather", methods=["POST"])
def third_party_gather():
    """Handle the third party's spoken response and continue the conversation."""
    job_id = request.args.get("job_id", "")
    call_sid = request.values.get("CallSid", "")
    speech = (request.values.get("SpeechResult") or "").strip()

    resp = VoiceResponse()

    job = db_exec("SELECT * FROM third_party_calls WHERE id=%s", (job_id,), fetchone=True)
    if not job:
        resp.hangup()
        return _twiml(resp)

    agent = db_exec("SELECT * FROM agents WHERE id=%s", (str(job["agent_id"]),), fetchone=True)
    if not agent or not speech:
        resp.hangup()
        return _twiml(resp)

    voice = agent.get("voice") or DEFAULT_VOICE
    append_transcript(call_sid, "third_party", speech)

    bridge.queue_message(str(agent["id"]), call_sid, f"[THIRD PARTY SAYS]: {speech}")
    result, end_call, _ = bridge.poll_result(call_sid)
    append_transcript(call_sid, "agent", result)

    if end_call:
        _say(resp, result, voice)
        resp.hangup()
        _complete_third_party(job_id, call_sid, agent)
    else:
        g = resp.gather(
            input="speech",
            action=_url("/webhooks/twilio/third-party-gather", job_id=job_id),
            method="POST",
            timeout=8,
            speech_timeout="auto",
        )
        _say(g, result, voice)
        resp.hangup()

    return _twiml(resp)


def _complete_third_party(job_id: str, call_sid: str, agent: dict | None):
    """
    Mark a third-party call as completed, save transcript, and notify the agent.
    Called when the agent signals end_call=True.
    """
    turns = get_transcript(call_sid)
    transcript_str = json.dumps(turns) if turns else None

    db_exec(
        """
        UPDATE third_party_calls
        SET status='completed', completed_at=NOW(), transcript=%s
        WHERE id=%s
        """,
        (transcript_str, job_id),
    )

    # Notify agent via listen queue so no public webhook URL is needed
    if agent:
        import json as _json
        notification = _json.dumps({
            "job_id": job_id,
            "status": "completed",
            "transcript": turns,
        })
        bridge.queue_message(
            str(agent["id"]),
            f"notify-{job_id}",
            f"[THIRD PARTY COMPLETE]\n{notification}",
        )
