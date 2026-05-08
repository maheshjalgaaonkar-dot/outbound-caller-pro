import os
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

_client: Optional[Client] = None


def get_db() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _client = create_client(url, key)
    return _client


# ─── Contacts ─────────────────────────────────────────────────────────────────

def upsert_contact(phone: str, name: str = None, email: str = None) -> dict:
    db = get_db()
    data = {"phone": phone}
    if name:
        data["name"] = name
    if email:
        data["email"] = email
    res = db.table("contacts").upsert(data, on_conflict="phone").execute()
    return res.data[0] if res.data else {}


def get_contact_by_phone(phone: str) -> Optional[dict]:
    db = get_db()
    res = db.table("contacts").select("*").eq("phone", phone).limit(1).execute()
    return res.data[0] if res.data else None


def update_contact_memory(contact_id: str, ai_memory: str):
    db = get_db()
    db.table("contacts").update({"ai_memory": ai_memory}).eq("id", contact_id).execute()


def update_contact_notes(contact_id: str, notes: str):
    db = get_db()
    db.table("contacts").update({"notes": notes}).eq("id", contact_id).execute()


# ─── Call Logs ────────────────────────────────────────────────────────────────

def create_call_log(phone: str, room_name: str, contact_id: str = None,
                    campaign_id: str = None, agent_profile_id: str = None) -> dict:
    db = get_db()
    data = {
        "phone": phone,
        "room_name": room_name,
        "direction": "outbound",
        "status": "initiated",
    }
    if contact_id:
        data["contact_id"] = contact_id
    if campaign_id:
        data["campaign_id"] = campaign_id
    if agent_profile_id:
        data["agent_profile_id"] = agent_profile_id
    res = db.table("call_logs").insert(data).execute()
    return res.data[0] if res.data else {}


def update_call_log(call_log_id: str, **kwargs):
    db = get_db()
    db.table("call_logs").update(kwargs).eq("id", call_log_id).execute()


def get_call_logs(limit: int = 100, offset: int = 0) -> list:
    db = get_db()
    res = (db.table("call_logs")
           .select("*, contacts(name,phone), campaigns(name), agent_profiles(name)")
           .order("started_at", desc=True)
           .range(offset, offset + limit - 1)
           .execute())
    return res.data or []


# ─── Agent Profiles ───────────────────────────────────────────────────────────

def get_agent_profile(profile_id: str = None, name: str = None) -> Optional[dict]:
    db = get_db()
    q = db.table("agent_profiles").select("*")
    if profile_id:
        q = q.eq("id", profile_id)
    elif name:
        q = q.eq("name", name)
    else:
        q = q.eq("name", "Default Agent")
    res = q.limit(1).execute()
    return res.data[0] if res.data else None


def list_agent_profiles() -> list:
    db = get_db()
    return db.table("agent_profiles").select("*").order("created_at").execute().data or []


# ─── Campaigns ────────────────────────────────────────────────────────────────

def get_campaign(campaign_id: str) -> Optional[dict]:
    db = get_db()
    res = db.table("campaigns").select("*, agent_profiles(*)").eq("id", campaign_id).limit(1).execute()
    return res.data[0] if res.data else None


def list_campaigns() -> list:
    db = get_db()
    return (db.table("campaigns")
            .select("*, agent_profiles(name)")
            .order("created_at", desc=True)
            .execute().data or [])


def update_campaign_status(campaign_id: str, status: str):
    db = get_db()
    db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()


def get_pending_campaign_contacts(campaign_id: str) -> list:
    db = get_db()
    res = (db.table("campaign_contacts")
           .select("*, contacts(*)")
           .eq("campaign_id", campaign_id)
           .eq("status", "pending")
           .execute())
    return res.data or []


def mark_campaign_contact(cc_id: str, status: str):
    import datetime
    db = get_db()
    db.table("campaign_contacts").update(
        {"status": status, "called_at": datetime.datetime.utcnow().isoformat()}
    ).eq("id", cc_id).execute()


def increment_campaign_called(campaign_id: str):
    db = get_db()
    db.rpc("increment_campaign_called", {"cid": campaign_id}).execute()


# ─── Appointments ─────────────────────────────────────────────────────────────

def create_appointment(contact_id: str, title: str, scheduled_at: str,
                        notes: str = "", call_log_id: str = None,
                        calcom_booking_id: str = None) -> dict:
    db = get_db()
    data = {
        "contact_id": contact_id,
        "title": title,
        "scheduled_at": scheduled_at,
        "notes": notes,
    }
    if call_log_id:
        data["call_log_id"] = call_log_id
    if calcom_booking_id:
        data["calcom_booking_id"] = calcom_booking_id
    res = db.table("appointments").insert(data).execute()
    return res.data[0] if res.data else {}


def list_appointments(limit: int = 100) -> list:
    db = get_db()
    return (db.table("appointments")
            .select("*, contacts(name,phone)")
            .order("scheduled_at", desc=False)
            .limit(limit)
            .execute().data or [])


# ─── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    db = get_db()
    res = db.table("settings").select("value").eq("key", key).limit(1).execute()
    return res.data[0]["value"] if res.data else None


def set_setting(key: str, value: str):
    db = get_db()
    db.table("settings").upsert({"key": key, "value": value}).execute()


# ─── Dashboard Stats ──────────────────────────────────────────────────────────

def get_dashboard_stats() -> dict:
    db = get_db()
    total_calls = db.table("call_logs").select("id", count="exact").execute().count or 0
    answered = (db.table("call_logs").select("id", count="exact")
                .eq("status", "answered").execute().count or 0)
    completed = (db.table("call_logs").select("id", count="exact")
                 .eq("status", "completed").execute().count or 0)
    total_contacts = db.table("contacts").select("id", count="exact").execute().count or 0
    total_appointments = db.table("appointments").select("id", count="exact").execute().count or 0
    active_campaigns = (db.table("campaigns").select("id", count="exact")
                        .eq("status", "running").execute().count or 0)

    # Calls per day (last 7 days) using raw SQL via rpc or just last 50 logs
    recent_logs = (db.table("call_logs")
                   .select("started_at, status")
                   .order("started_at", desc=True)
                   .limit(200)
                   .execute().data or [])

    from collections import defaultdict
    import datetime
    daily: dict = defaultdict(int)
    for log in recent_logs:
        if log.get("started_at"):
            day = log["started_at"][:10]
            daily[day] += 1

    return {
        "total_calls": total_calls,
        "answered": answered,
        "completed": completed,
        "total_contacts": total_contacts,
        "total_appointments": total_appointments,
        "active_campaigns": active_campaigns,
        "answer_rate": round((answered / total_calls * 100) if total_calls else 0, 1),
        "daily_calls": dict(sorted(daily.items())[-7:]),
    }
