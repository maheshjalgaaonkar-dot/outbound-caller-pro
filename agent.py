import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import time
import datetime
from dotenv import load_dotenv

from google.genai.types import HttpOptions
from livekit import api as lkapi
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool

try:
    from livekit.plugins.google import realtime as _google_realtime
except ImportError:
    from livekit.plugins.google.beta import realtime as _google_realtime

import db

load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aiona-agent")
logger.info("=== aiona-agent starting up — livekit imports OK ===")


# ─── Agent (tools defined as methods — livekit-agents 1.x API) ───────────────

class AionaAgent(Agent):
    def __init__(self, *, instructions: str, phone: str,
                 contact_id: str, call_log_id: str) -> None:
        super().__init__(instructions=instructions)
        self._phone = phone
        self._contact_id = contact_id
        self._call_log_id = call_log_id

    @function_tool
    async def book_appointment(self, title: str, scheduled_at: str,
                                notes: str = "") -> str:
        """Book an appointment for the contact. Use this when the caller agrees
        to a meeting or follow-up. Provide a title, ISO datetime string for
        scheduled_at (e.g. '2025-06-15T10:00:00'), and optional notes."""
        try:
            db.create_appointment(
                contact_id=self._contact_id,
                title=title,
                scheduled_at=scheduled_at,
                notes=notes,
                call_log_id=self._call_log_id,
            )
            calcom_key = os.getenv("CALCOM_API_KEY")
            calcom_event_id = os.getenv("CALCOM_EVENT_TYPE_ID")
            if calcom_key and calcom_event_id and self._phone:
                try:
                    import httpx
                    payload = {
                        "eventTypeId": int(calcom_event_id),
                        "start": scheduled_at,
                        "responses": {
                            "name": "Lead",
                            "email": "lead@example.com",
                            "location": {"optionValue": "", "value": "phone"},
                        },
                        "timeZone": "Asia/Kolkata",
                        "language": "en",
                        "metadata": {"phone": self._phone},
                    }
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            "https://api.cal.com/v1/bookings",
                            params={"apiKey": calcom_key},
                            json=payload,
                            timeout=10,
                        )
                except Exception as e:
                    logger.warning(f"Cal.com sync failed: {e}")
            return f"Appointment '{title}' booked for {scheduled_at}."
        except Exception as e:
            logger.error(f"book_appointment error: {e}")
            return f"Failed to book appointment: {e}"

    @function_tool
    def lookup_contact_memory(self) -> str:
        """Look up key facts and prior conversation notes about this contact from the CRM."""
        try:
            contact = db.get_contact_by_phone(self._phone)
            if not contact:
                return "No prior history found for this contact."
            parts = []
            if contact.get("name"):
                parts.append(f"Name: {contact['name']}")
            if contact.get("ai_memory"):
                parts.append(f"Memory: {contact['ai_memory']}")
            if contact.get("notes"):
                parts.append(f"Notes: {contact['notes']}")
            return "\n".join(parts) if parts else "Contact exists but no memory yet."
        except Exception as e:
            return f"Error fetching memory: {e}"

    @function_tool
    def save_memory(self, fact: str) -> str:
        """Save an important fact or summary about this call to the contact's CRM memory.
        Use this at the end of the call or when you learn something significant."""
        try:
            contact = db.get_contact_by_phone(self._phone)
            if not contact:
                return "Cannot save — contact not found."
            existing = contact.get("ai_memory", "") or ""
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            updated = f"{existing}\n[{timestamp}] {fact}".strip()
            db.update_contact_memory(contact["id"], updated)
            return "Memory saved."
        except Exception as e:
            return f"Error saving memory: {e}"


# ─── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    logger.info(f"Room: {ctx.room.name}")

    meta: dict = {}
    for raw in [ctx.job.metadata if ctx.job else None, ctx.room.metadata]:
        if raw:
            try:
                meta.update(json.loads(raw))
            except Exception:
                pass

    phone_number: str = meta.get("phone_number", "")
    campaign_id: str = meta.get("campaign_id", "")
    agent_profile_id: str = meta.get("agent_profile_id", "")

    profile = db.get_agent_profile(profile_id=agent_profile_id or None)
    if not profile:
        logger.error("No agent profile found — aborting.")
        return

    system_prompt: str = profile["system_prompt"]
    # Env vars take priority over profile defaults — set GEMINI_MODEL / GEMINI_TTS_VOICE in Coolify to override globally.
    gemini_model: str = os.getenv("GEMINI_MODEL") or profile.get("model") or "gemini-2.5-flash-native-audio-latest"
    voice: str = os.getenv("GEMINI_TTS_VOICE") or profile.get("voice") or "Puck"
    use_realtime: bool = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"
    logger.info(f"model={gemini_model} voice={voice} realtime={use_realtime}")

    contact_id = ""
    call_log_id = ""
    if phone_number:
        contact = db.upsert_contact(phone_number)
        contact_id = contact.get("id", "")
        call_log = db.create_call_log(
            phone=phone_number,
            room_name=ctx.room.name,
            contact_id=contact_id or None,
            campaign_id=campaign_id or None,
            agent_profile_id=profile.get("id"),
        )
        call_log_id = call_log.get("id", "")

    call_start = time.time()

    session = AgentSession(
        llm=_google_realtime.RealtimeModel(
            model=gemini_model,
            voice=voice,
            instructions=system_prompt,
            temperature=0.8,
            http_options=HttpOptions(api_version="v1beta"),
        ),
    )

    await session.start(
        room=ctx.room,
        agent=AionaAgent(
            instructions=system_prompt,
            phone=phone_number,
            contact_id=contact_id,
            call_log_id=call_log_id,
        ),
    )

    # Dial out via Vobiz SIP trunk
    if phone_number:
        already_present = any(
            "sip_" in p.identity
            for p in ctx.room.remote_participants.values()
        )
        if not already_present:
            logger.info(f"Dialling {phone_number} via SIP trunk {os.getenv('VOBIZ_SIP_TRUNK_ID')}...")
            lk = lkapi.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL", ""),
                api_key=os.getenv("LIVEKIT_API_KEY", ""),
                api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
            )
            try:
                await lk.sip.create_sip_participant(
                    lkapi.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=os.getenv("VOBIZ_SIP_TRUNK_ID", ""),
                        sip_call_to=phone_number,
                        participant_identity=f"sip_{phone_number}",
                        wait_until_answered=True,
                    )
                )
                logger.info("Call answered — Gemini Live reactive, waiting for caller to speak.")
                if call_log_id:
                    db.update_call_log(call_log_id, status="answered")
            except Exception as e:
                logger.error(f"SIP dial failed: {e}")
                if call_log_id:
                    db.update_call_log(call_log_id, status="failed")
            finally:
                await lk.aclose()

    @ctx.room.on("disconnected")
    def on_disconnected(*_):
        duration = int(time.time() - call_start)
        if call_log_id:
            db.update_call_log(
                call_log_id,
                status="completed",
                duration_seconds=duration,
                ended_at=datetime.datetime.utcnow().isoformat(),
            )
        if campaign_id and contact_id:
            try:
                cc_rows = (db.get_db()
                           .table("campaign_contacts")
                           .select("id")
                           .eq("campaign_id", campaign_id)
                           .eq("contact_id", contact_id)
                           .limit(1)
                           .execute().data or [])
                if cc_rows:
                    db.mark_campaign_contact(cc_rows[0]["id"], "answered")
            except Exception as ex:
                logger.warning(f"campaign_contact update failed: {ex}")
        logger.info(f"Call ended. Duration: {duration}s")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="aiona-caller",
        )
    )
