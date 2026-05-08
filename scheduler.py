import os
import json
import random
import asyncio
import logging
import datetime
from typing import Optional

from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from livekit import api as lkapi

import db

load_dotenv()
logger = logging.getLogger("aiona-scheduler")

_scheduler: Optional[BackgroundScheduler] = None


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _lk() -> lkapi.LiveKitAPI:
    return lkapi.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL", ""),
        api_key=os.getenv("LIVEKIT_API_KEY", ""),
        api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
    )


def _dispatch_call_sync(phone: str, campaign_id: str, agent_profile_id: str):
    """Sync wrapper — runs async dispatch in a new event loop."""
    async def _inner():
        lk = _lk()
        room_name = f"call-{phone.replace('+','')}--{random.randint(10000,99999)}"
        meta = {
            "phone_number": phone,
            "campaign_id": campaign_id,
            "agent_profile_id": agent_profile_id,
        }
        try:
            await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name="aiona-caller",
                    room=room_name,
                    metadata=json.dumps(meta),
                )
            )
            logger.info(f"Dispatched call to {phone} (campaign={campaign_id})")
        finally:
            await lk.aclose()

    asyncio.run(_inner())


def _execute_campaign(campaign_id: str):
    """Called by APScheduler — dispatches all pending contacts in the campaign."""
    logger.info(f"[Scheduler] Executing campaign {campaign_id}")
    db.update_campaign_status(campaign_id, "running")

    pending = db.get_pending_campaign_contacts(campaign_id)
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        logger.error(f"Campaign {campaign_id} not found")
        db.update_campaign_status(campaign_id, "failed")
        return

    profile_id = campaign.get("agent_profile_id") or ""

    for cc in pending:
        contact = cc.get("contacts") or {}
        phone = contact.get("phone", "")
        if not phone:
            continue
        try:
            _dispatch_call_sync(phone, campaign_id, profile_id)
            db.mark_campaign_contact(cc["id"], "called")
        except Exception as e:
            logger.error(f"Failed to dispatch {phone}: {e}")
            db.mark_campaign_contact(cc["id"], "failed")

    db.update_campaign_status(campaign_id, "completed")
    logger.info(f"[Scheduler] Campaign {campaign_id} completed.")


# ─── Public API ───────────────────────────────────────────────────────────────

def run_campaign_now(campaign_id: str):
    """Trigger a campaign immediately (used by API /run endpoint)."""
    if _scheduler:
        _scheduler.add_job(
            _execute_campaign,
            trigger=DateTrigger(run_date=datetime.datetime.now()),
            args=[campaign_id],
            id=f"manual-{campaign_id}",
            replace_existing=True,
        )
    else:
        _execute_campaign(campaign_id)


def schedule_campaign(campaign_id: str, schedule_type: str, scheduled_at: str):
    """Register a campaign in the scheduler based on its schedule_type."""
    if not _scheduler:
        logger.warning("Scheduler not started — cannot register campaign.")
        return

    job_id = f"campaign-{campaign_id}"

    if schedule_type == "once":
        run_date = datetime.datetime.fromisoformat(scheduled_at)
        _scheduler.add_job(
            _execute_campaign,
            trigger=DateTrigger(run_date=run_date),
            args=[campaign_id],
            id=job_id,
            replace_existing=True,
        )
        logger.info(f"Scheduled campaign {campaign_id} once at {run_date}")

    elif schedule_type == "daily":
        # scheduled_at is just the time: "HH:MM" or full ISO
        dt = datetime.datetime.fromisoformat(scheduled_at)
        _scheduler.add_job(
            _execute_campaign,
            trigger=CronTrigger(hour=dt.hour, minute=dt.minute),
            args=[campaign_id],
            id=job_id,
            replace_existing=True,
        )
        logger.info(f"Scheduled campaign {campaign_id} daily at {dt.hour}:{dt.minute:02d}")

    elif schedule_type == "weekdays":
        dt = datetime.datetime.fromisoformat(scheduled_at)
        _scheduler.add_job(
            _execute_campaign,
            trigger=CronTrigger(day_of_week="mon-fri", hour=dt.hour, minute=dt.minute),
            args=[campaign_id],
            id=job_id,
            replace_existing=True,
        )
        logger.info(f"Scheduled campaign {campaign_id} weekdays at {dt.hour}:{dt.minute:02d}")


def _load_scheduled_campaigns():
    """Load all non-completed campaigns from DB and register them."""
    try:
        campaigns = db.get_db().table("campaigns").select("*").neq("status", "completed").execute().data or []
        for c in campaigns:
            if c.get("scheduled_at") and c.get("schedule_type") and c.get("status") != "running":
                schedule_campaign(c["id"], c["schedule_type"], c["scheduled_at"])
    except Exception as e:
        logger.warning(f"Could not load scheduled campaigns: {e}")


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()
    _load_scheduled_campaigns()
    logger.info("APScheduler started.")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped.")
