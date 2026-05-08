import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import asyncio
import csv
import io
import json
import random
import logging
import datetime
from contextlib import asynccontextmanager
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from livekit import api as lkapi

import db
import scheduler as sched_module

# load_dotenv is safe here: it does NOT override env vars already set by the VPS/Coolify.
load_dotenv(override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aiona-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sched_module.start_scheduler()
    logger.info("Aiona Voice API started.")
    yield
    sched_module.stop_scheduler()


app = FastAPI(title="Aiona Voice API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── LiveKit helper ───────────────────────────────────────────────────────────

def _lk() -> lkapi.LiveKitAPI:
    return lkapi.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL", ""),
        api_key=os.getenv("LIVEKIT_API_KEY", ""),
        api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
    )


async def _dispatch_call(phone: str, campaign_id: str = None,
                          agent_profile_id: str = None) -> dict:
    lk = _lk()
    room_name = f"call-{phone.replace('+','')}--{random.randint(10000,99999)}"
    meta = {"phone_number": phone}
    if campaign_id:
        meta["campaign_id"] = campaign_id
    if agent_profile_id:
        meta["agent_profile_id"] = agent_profile_id

    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="aiona-caller",
                room=room_name,
                metadata=json.dumps(meta),
            )
        )
        return {"dispatch_id": dispatch.id, "room": room_name}
    finally:
        await lk.aclose()


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class SingleCallRequest(BaseModel):
    phone: str
    agent_profile_id: Optional[str] = None

class CampaignCreate(BaseModel):
    name: str
    agent_profile_id: Optional[str] = None
    schedule_type: str = "once"
    scheduled_at: Optional[str] = None

class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    agent_profile_id: Optional[str] = None
    schedule_type: Optional[str] = None
    scheduled_at: Optional[str] = None
    status: Optional[str] = None

class AgentProfileCreate(BaseModel):
    name: str
    system_prompt: str
    initial_greeting: str = "Hello! This is Aiona calling."
    model: str = "gemini-2.0-flash-live-001"
    voice: str = "Puck"
    language: str = "en-US"

class AgentProfileUpdate(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    initial_greeting: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = None
    language: Optional[str] = None

class ContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None

class AppointmentCreate(BaseModel):
    contact_id: str
    title: str
    scheduled_at: str
    notes: str = ""
    call_log_id: Optional[str] = None

class SettingItem(BaseModel):
    key: str
    value: str


# ─── Dashboard HTML ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read()
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


# ─── Single Call ──────────────────────────────────────────────────────────────

@app.post("/call/single")
async def single_call(req: SingleCallRequest):
    if not req.phone.startswith("+"):
        raise HTTPException(400, "Phone must start with country code, e.g. +91...")
    result = await _dispatch_call(req.phone, agent_profile_id=req.agent_profile_id)
    return {"ok": True, **result}


# ─── Batch Call (CSV) ─────────────────────────────────────────────────────────

@app.post("/call/batch")
async def batch_call(
    file: UploadFile = File(...),
    agent_profile_id: Optional[str] = Query(None),
):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    dispatched = []
    errors = []
    for row in reader:
        phone = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
        if not phone:
            continue
        if not phone.startswith("+"):
            errors.append(f"Skipped {phone}: no country code")
            continue
        try:
            r = await _dispatch_call(phone, agent_profile_id=agent_profile_id)
            dispatched.append({"phone": phone, **r})
        except Exception as e:
            errors.append(f"{phone}: {e}")
    return {"dispatched": len(dispatched), "errors": errors, "calls": dispatched}


# ─── Campaigns ────────────────────────────────────────────────────────────────

@app.get("/campaigns")
def list_campaigns():
    return db.list_campaigns()


@app.post("/campaigns")
def create_campaign(req: CampaignCreate):
    d = db.get_db()
    data = req.model_dump(exclude_none=True)
    res = d.table("campaigns").insert(data).execute()
    return res.data[0] if res.data else {}


@app.put("/campaigns/{cid}")
def update_campaign(cid: str, req: CampaignUpdate):
    d = db.get_db()
    data = req.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "Nothing to update")
    d.table("campaigns").update(data).eq("id", cid).execute()
    return {"ok": True}


@app.delete("/campaigns/{cid}")
def delete_campaign(cid: str):
    db.get_db().table("campaigns").delete().eq("id", cid).execute()
    return {"ok": True}


@app.post("/campaigns/{cid}/contacts")
async def upload_campaign_contacts(cid: str, file: UploadFile = File(...)):
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    added = 0
    for row in reader:
        phone = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
        name  = (row.get("name") or row.get("Name") or "").strip() or None
        email = (row.get("email") or row.get("Email") or "").strip() or None
        if not phone:
            continue
        contact = db.upsert_contact(phone, name, email)
        if not contact.get("id"):
            continue
        try:
            db.get_db().table("campaign_contacts").upsert(
                {"campaign_id": cid, "contact_id": contact["id"], "status": "pending"},
                on_conflict="campaign_id,contact_id",
            ).execute()
            added += 1
        except Exception:
            pass
    total = db.get_db().table("campaign_contacts").select("id", count="exact").eq("campaign_id", cid).execute().count or 0
    db.get_db().table("campaigns").update({"total_contacts": total}).eq("id", cid).execute()
    return {"added": added, "total": total}


@app.post("/campaigns/{cid}/run")
async def run_campaign_now(cid: str):
    campaign = db.get_campaign(cid)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    sched_module.run_campaign_now(cid)
    return {"ok": True, "message": f"Campaign '{campaign['name']}' triggered."}


@app.get("/campaigns/{cid}/contacts")
def get_campaign_contacts(cid: str):
    d = db.get_db()
    res = (d.table("campaign_contacts")
           .select("*, contacts(name,phone,email)")
           .eq("campaign_id", cid)
           .execute())
    return res.data or []


# ─── Call Logs ────────────────────────────────────────────────────────────────

@app.get("/call-logs")
def get_call_logs(limit: int = 100, offset: int = 0):
    return db.get_call_logs(limit, offset)


@app.get("/call-logs/{lid}")
def get_call_log(lid: str):
    res = db.get_db().table("call_logs").select("*").eq("id", lid).limit(1).execute()
    if not res.data:
        raise HTTPException(404)
    return res.data[0]


# ─── CRM / Contacts ───────────────────────────────────────────────────────────

@app.get("/contacts")
def list_contacts(limit: int = 100, offset: int = 0, search: str = ""):
    d = db.get_db()
    q = d.table("contacts").select("*").order("created_at", desc=True)
    if search:
        q = q.or_(f"name.ilike.%{search}%,phone.ilike.%{search}%")
    return q.range(offset, offset + limit - 1).execute().data or []


@app.get("/contacts/{cid}")
def get_contact(cid: str):
    res = db.get_db().table("contacts").select("*").eq("id", cid).limit(1).execute()
    if not res.data:
        raise HTTPException(404)
    return res.data[0]


@app.put("/contacts/{cid}")
def update_contact(cid: str, req: ContactUpdate):
    data = req.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "Nothing to update")
    db.get_db().table("contacts").update(data).eq("id", cid).execute()
    return {"ok": True}


@app.delete("/contacts/{cid}")
def delete_contact(cid: str):
    db.get_db().table("contacts").delete().eq("id", cid).execute()
    return {"ok": True}


@app.get("/contacts/{cid}/call-logs")
def contact_call_logs(cid: str):
    return db.get_db().table("call_logs").select("*").eq("contact_id", cid).order("started_at", desc=True).execute().data or []


# ─── Agent Profiles ───────────────────────────────────────────────────────────

@app.get("/agent-profiles")
def list_profiles():
    return db.list_agent_profiles()


@app.post("/agent-profiles")
def create_profile(req: AgentProfileCreate):
    d = db.get_db()
    res = d.table("agent_profiles").insert(req.model_dump()).execute()
    return res.data[0] if res.data else {}


@app.put("/agent-profiles/{pid}")
def update_profile(pid: str, req: AgentProfileUpdate):
    data = req.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400)
    db.get_db().table("agent_profiles").update(data).eq("id", pid).execute()
    return {"ok": True}


@app.delete("/agent-profiles/{pid}")
def delete_profile(pid: str):
    db.get_db().table("agent_profiles").delete().eq("id", pid).execute()
    return {"ok": True}


# ─── Appointments ─────────────────────────────────────────────────────────────

@app.get("/appointments")
def list_appointments(limit: int = 100):
    return db.list_appointments(limit)


@app.post("/appointments")
def create_appointment(req: AppointmentCreate):
    return db.create_appointment(**req.model_dump(exclude_none=True))


@app.delete("/appointments/{aid}")
def delete_appointment(aid: str):
    db.get_db().table("appointments").delete().eq("id", aid).execute()
    return {"ok": True}


# ─── Settings (read-only view of VPS environment) ────────────────────────────
# Single source of truth is the VPS/Coolify environment variables.
# This endpoint only READS from os.environ — never writes.
# To change a setting, update the env var in Coolify (or your VPS systemd/shell)
# and restart the containers.

EXPOSED_SETTINGS = [
    "GOOGLE_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "VOBIZ_SIP_TRUNK_ID", "VOBIZ_SIP_DOMAIN", "VOBIZ_OUTBOUND_NUMBER",
    "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
    "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID",
    "S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY",
]

SENSITIVE_KEYS = {
    "GOOGLE_API_KEY", "LIVEKIT_API_SECRET", "SUPABASE_SERVICE_KEY",
    "S3_ACCESS_KEY", "S3_SECRET_KEY", "CALCOM_API_KEY",
}


@app.get("/settings")
def get_settings():
    result = {}
    for key in EXPOSED_SETTINGS:
        val = os.environ.get(key, "")
        if val and key in SENSITIVE_KEYS:
            result[key] = val[:4] + "*" * max(0, len(val) - 4)
        else:
            result[key] = val
    return result


@app.get("/settings/status")
def settings_status():
    """Returns which required env vars are set vs missing — useful for deployment checks."""
    required = ["GOOGLE_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
                "VOBIZ_SIP_TRUNK_ID", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    return {
        key: ("set" if os.environ.get(key) else "MISSING")
        for key in required
    }


# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ─── Dashboard Stats ──────────────────────────────────────────────────────────

@app.get("/dashboard/stats")
def dashboard_stats():
    return db.get_dashboard_stats()


# ─── LiveKit Webhook (call recording) ────────────────────────────────────────

@app.post("/webhooks/livekit")
async def livekit_webhook(request_body: dict):
    event = request_body.get("event", "")
    if event == "egress_ended":
        info = request_body.get("egressInfo", {})
        room_name = info.get("roomName", "")
        file_url = ""
        for seg in info.get("fileResults", []):
            file_url = seg.get("location", "")
            break
        if room_name and file_url:
            d = db.get_db()
            rows = d.table("call_logs").select("id").eq("room_name", room_name).limit(1).execute().data or []
            if rows:
                db.update_call_log(rows[0]["id"], recording_url=file_url)
    return {"ok": True}


# ─── Live Logs (SSE) ─────────────────────────────────────────────────────────

_live_log_clients: list = []

async def _sse_stream():
    q: asyncio.Queue = asyncio.Queue()
    _live_log_clients.append(q)
    try:
        while True:
            msg = await q.get()
            yield f"data: {msg}\n\n"
    except asyncio.CancelledError:
        _live_log_clients.remove(q)


@app.get("/logs/stream")
async def stream_logs():
    return StreamingResponse(_sse_stream(), media_type="text/event-stream")


def broadcast_log(message: str):
    for q in list(_live_log_clients):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("API_PORT", 8000)), reload=False)
