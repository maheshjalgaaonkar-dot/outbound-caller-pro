import os
import certifi

# Fix for macOS SSL Certificate errors - MUST be before other imports
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import time
import datetime
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import google, silero, noise_cancellation
from livekit.agents import llm

import db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aiona-agent")


# ─── Tools ────────────────────────────────────────────────────────────────────

class AionaTools(llm.ToolContext):
    def __init__(self, ctx: agents.JobContext, phone: str, contact_id: str,
                 call_log_id: str):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone = phone
        self.contact_id = contact_id
        self.call_log_id = call_log_id

    @llm.function_tool(description=(
        "Book an appointment for the contact. Use this when the caller agrees "
        "to a meeting or follow-up. Provide a title, ISO datetime string for "
        "scheduled_at (e.g. '2025-06-15T10:00:00'), and optional notes."
    ))
    async def book_appointment(self, title: str, scheduled_at: str,
                                notes: str = "") -> str:
        try:
            appt = db.create_appointment(
                contact_id=self.contact_id,
                title=title,
                scheduled_at=scheduled_at,
                notes=notes,
                call_log_id=self.call_log_id,
            )

            # Optionally sync to Cal.com
            calcom_key = os.getenv("CALCOM_API_KEY")
            calcom_event_id = os.getenv("CALCOM_EVENT_TYPE_ID")
            if calcom_key and calcom_event_id and self.phone:
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
                        "metadata": {"phone": self.phone},
                    }
                    async with httpx.AsyncClient() as client:
                        r = await client.post(
                            "https://api.cal.com/v1/bookings",
                            params={"apiKey": calcom_key},
                            json=payload,
                            timeout=10,
                        )
                        if r.status_code in (200, 201):
                            booking = r.json()
                            db.update_call_log(appt["id"],
                                calcom_booking_id=str(booking.get("id", "")))
                except Exception as e:
                    logger.warning(f"Cal.com sync failed: {e}")

            return f"Appointment '{title}' booked for {scheduled_at}."
        except Exception as e:
            logger.error(f"book_appointment error: {e}")
            return f"Failed to book appointment: {e}"

    @llm.function_tool(description=(
        "Look up key facts and notes about this contact from the CRM."
    ))
    def lookup_contact_memory(self) -> str:
        try:
            contact = db.get_contact_by_phone(self.phone)
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

    @llm.function_tool(description=(
        "Save an important fact or summary about this call to the contact's CRM memory. "
        "Use this at the end of the call or when you learn something significant."
    ))
    def save_memory(self, fact: str) -> str:
        try:
            contact = db.get_contact_by_phone(self.phone)
            if not contact:
                return "Cannot save — contact not found."
            existing = contact.get("ai_memory", "") or ""
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            updated = f"{existing}\n[{timestamp}] {fact}".strip()
            db.update_contact_memory(contact["id"], updated)
            return "Memory saved."
        except Exception as e:
            return f"Error saving memory: {e}"


# ─── Agent ────────────────────────────────────────────────────────────────────

class AionaAgent(Agent):
    def __init__(self, instructions: str, tools: list) -> None:
        super().__init__(instructions=instructions, tools=tools)


# ─── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext):
    logger.info(f"Connecting to room: {ctx.room.name}")

    # Parse metadata from job or room (room overrides job)
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

    # Resolve agent profile
    profile = db.get_agent_profile(profile_id=agent_profile_id or None)
    if not profile:
        logger.error("No agent profile found. Shutting down.")
        ctx.shutdown()
        return

    system_prompt: str = profile["system_prompt"]
    initial_greeting: str = profile["initial_greeting"]
    gemini_model: str = profile.get("model", os.getenv("GEMINI_MODEL", "gemini-2.0-flash-live-001"))
    voice: str = profile.get("voice", "Puck")
    language: str = profile.get("language", "en-US")

    # Upsert contact and create call log
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

    tools_ctx = AionaTools(ctx, phone_number, contact_id, call_log_id)

    # Build Gemini Live session — system prompt passed as `instructions` to RealtimeModel
    session = AgentSession(
        vad=silero.VAD.load(),
        llm=google.realtime.RealtimeModel(
            model=gemini_model,
            voice=voice,
            instructions=system_prompt,
            temperature=0.8,
        ),
    )

    await session.start(
        room=ctx.room,
        agent=AionaAgent(
            instructions=system_prompt,
            tools=list(tools_ctx.function_tools.values()),
        ),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )

    # Dial out if phone not already present
    should_dial = False
    if phone_number:
        already_present = any(
            "sip_" in p.identity
            for p in ctx.room.remote_participants.values()
        )
        if not already_present:
            should_dial = True

    if should_dial:
        logger.info(f"Dialling {phone_number} via SIP trunk {os.getenv('VOBIZ_SIP_TRUNK_ID')}...")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=os.getenv("VOBIZ_SIP_TRUNK_ID", ""),
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered.")
            if call_log_id:
                db.update_call_log(call_log_id, status="answered")

            # Gemini Live is REACTIVE — do NOT call generate_reply() here.
            # The agent will respond naturally when the person first speaks.

        except Exception as e:
            logger.error(f"SIP dial failed: {e}")
            if call_log_id:
                db.update_call_log(call_log_id, status="failed")
            ctx.shutdown()
            return

    # Register disconnect handler to finalize the call log
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
        # Update campaign_contacts if campaign-driven
        if campaign_id:
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
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="aiona-caller",
        )
    )
