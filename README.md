# ClawCall

**Phone calls for AI agents.**

ClawCall gives every [OpenClaw](https://github.com/steipete/OpenClaw) agent a real phone number. Users call their agent, the agent calls them back, schedules briefings, and places calls to third parties autonomously — all with zero credential setup for the user.

---

## What It Does

| Feature | Description |
|---|---|
| **Inbound calls** | User dials their agent's number and has a live voice conversation |
| **Task callbacks** | Agent calls the user back when a background task completes |
| **Scheduled calls** | Agent calls on a cron schedule (daily briefings, reminders) |
| **3rd party calling** | Agent calls someone else autonomously on the user's behalf |
| **Custom voice** | Per-agent Polly neural voice selection |
| **OpenAI Realtime** | Optional SIP handoff path for low-latency interruptible calls (`gpt-realtime`) |
| **Multi-agent** | Team tier supports up to 5 agents, each with a dedicated number |
| **Webhook push** | Team tier pushes call events to your own endpoint in real time |
| **Call recordings** | Every call recorded and URL stored in call history |
| **Transcripts** | Full turn-by-turn transcripts saved per call |

---

## How It Works

```
User dials ClawCall number
  → Twilio receives call, hits ClawCall webhook
  → ClawCall looks up agent from phone number
  → STT: Twilio transcribes speech to text
  → ClawCall POSTs text to agent's webhook URL
  → Agent responds with text + end_call flag
  → TTS: Polly speaks response back to caller
  → Loop continues until hangup
```

The agent only ever holds a `CLAWCALL_API_KEY`. Twilio credentials never leave the backend.

### Optional OpenAI Realtime SIP path

For agents with `voice_provider='openai_realtime'`, ClawCall can accept OpenAI SIP call webhooks at:

```
POST /webhooks/openai/realtime/sip?agent_id=<agent_uuid>
```

The webhook extracts the OpenAI `call_id`, verifies the agent is opted into the realtime provider, and calls `OpenAI().realtime.calls.accept(...)` with `gpt-realtime` and a natural voice (`marin` by default). Configure these env vars when deploying the path:

```
OPENAI_API_KEY=...
OPENAI_REALTIME_MODEL=gpt-realtime
OPENAI_REALTIME_VOICE=marin
```

This bypasses the old Twilio speech-to-text → agent webhook → Polly `<Say>` loop for calls routed through OpenAI SIP.

---

## Stack

- **Runtime:** Python 3.11+ / Flask
- **Database:** PostgreSQL
- **Scheduler:** APScheduler
- **Telephony:** Twilio Voice + Recordings
- **TTS:** Amazon Polly (neural voices via Twilio)
- **Payments:** USDC / USDT on Base mainnet (no Stripe)

---

## API

### Registration
```
POST /api/v1/register
```
Agent self-registers with email + webhook URL. Returns `api_key` and assigned phone number.

### Calls
```
POST /api/v1/calls/outbound/callback     # call user when task completes
POST /api/v1/calls/outbound/third-party  # call a third party autonomously
POST /api/v1/calls/schedule              # create a recurring scheduled call
DELETE /api/v1/calls/schedule/:id        # remove a schedule
GET  /api/v1/calls/history               # call logs + transcripts
```

### Account
```
GET  /api/v1/account                # tier, usage, phone number
POST /api/v1/account/phone          # set personal phone for callbacks
POST /api/v1/account/voice          # set Polly voice
POST /api/v1/account/webhook        # set webhook push URL (Team)
```

### Multi-agent (Team)
```
GET    /api/v1/agents               # list all agents
POST   /api/v1/agents               # add agent (provisions dedicated number)
PATCH  /api/v1/agents/:id           # update name / webhook URL
DELETE /api/v1/agents/:id           # remove agent + release number
```

### Billing
```
POST /api/v1/billing/checkout       # get payment address + amount
POST /api/v1/billing/verify         # verify on-chain tx, upgrade tier instantly
GET  /api/v1/billing/status         # plan, expiry, overage
POST /api/v1/billing/cancel         # cancel auto-renew
```

### Twilio Webhooks
```
POST /webhooks/twilio/inbound       # incoming call
POST /webhooks/twilio/status        # call status updates
POST /webhooks/twilio/recording     # recording URL when ready
WS   /webhooks/twilio/gather        # speech-to-agent bridge
```

---

## Tiers

| | Free | Pro ($9/mo) | Team ($29/mo) |
|---|---|---|---|
| Minutes/month | 10 | 120 | 500 pooled |
| Phone number | Shared | Dedicated | Dedicated ×5 |
| Outbound calls | — | ✓ | ✓ |
| Scheduled calls | — | ✓ | ✓ |
| 3rd party calling | — | ✓ | ✓ |
| Multi-agent | — | — | ✓ (5 agents) |
| Webhook push | — | — | ✓ |
| Overage | Blocked | $0.05/min (max +5 min) | $0.05/min (max +5 min) |

Payment accepted in **USDC or USDT on Base mainnet**.

---

## Agent Webhook Contract

ClawCall POSTs to your agent's webhook:

```
POST {agent_webhook_url}/clawcall/message

{
  "call_sid": "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "message": "Hey, what's the weather today?"
}
```

Your agent responds:

```json
{
  "response": "It's 72°F and sunny in San Francisco.",
  "end_call": false
}
```

Set `end_call: true` to hang up after speaking. Respond within 25 seconds — ClawCall plays filler phrases while waiting so the line stays active.

---

## Running Locally

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Set up .env with your credentials
python run.py
```

The server starts on `PORT` (default 3000). For Twilio webhooks to reach you locally, expose it with [ngrok](https://ngrok.com) or [Tailscale Funnel](https://tailscale.com/kb/1223/funnel).

---

## OpenClaw Skill

The `skill/` directory contains the ClawCall OpenClaw skill package:

- `SKILL.md` — agent instructions (registered on ClawHub)
- `clawhub.json` — skill manifest
- `references/setup.md` — full webhook contract and backend reference

Users install by telling their agent: **"Install clawcall"**

---

## License

MIT
