"""
Lease Agent Dashboard — Full Lead Management Interface

Features:
- Full lead status view: name, address, last message, last call, showing date, status
- AI-powered action decisions with message template library
- Voice bot trigger for scheduling showings
- Response time tracking per lead
- Periodic scan + instant response on lead activity

Usage:
    python dashboard.py
    → Opens at http://localhost:8000
"""

import os
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta

# Load .env file if present (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import base64
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, os.path.dirname(__file__))
from lease_agent import (
    get_all_opportunities, enrich_lead, ask_claude,
    send_sms, update_stage, add_contact_tag,
    get_unavailable_properties, _is_property_unavailable,
    STAGE_MAP, STAGE_NAME_TO_ID,
    LEASE_PIPELINE_ID, GHL_API_KEY, GHL_LOCATION_ID, OPENAI_API_KEY,
    ghl_headers, GHL_API_BASE,
    VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, VAPI_ASSISTANT_ID, VAPI_BASE,
    CALL_LOG_FILE, _check_call_history,
)
from message_templates import TEMPLATES, format_template, get_templates_for_stage
from response_engine import PeriodicScheduler, handle_inbound, is_business_hours
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI()

# Serve logo files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/logo1.png")
async def logo1():
    path = os.path.join(BASE_DIR, "LOGO 1.png")
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/png")

@app.get("/logo2.png")
async def logo2():
    path = os.path.join(BASE_DIR, "LOGO 2.png")
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type="image/png")

# Voice bot workflow — will be connected once added to GHL workflow
VOICE_BOT_AGENT_ID = "69d658fa4ccb41abc9c6f543"

# Periodic scheduler (mode set by SCHEDULER_MODE env var)
import os as _os
_scheduler_mode = _os.environ.get("SCHEDULER_MODE", "DRY_RUN").upper()
scheduler = PeriodicScheduler(dry_run=(_scheduler_mode != "LIVE"))

scan_cache: dict = {"leads": [], "scan_time": None, "scanning": False}
webhook_log: list = []  # Track all webhook calls for debugging


# ── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return DASHBOARD_HTML


@app.get("/api/templates")
async def api_templates():
    return JSONResponse(TEMPLATES)


SCAN_CONCURRENCY = 2  # Reduce from 5 to 2 to avoid rate limiting
RATE_LIMIT_DELAY = 0.5  # 500ms delay between AI calls


async def _process_one_opp(client: httpx.AsyncClient, opp: dict, semaphore: asyncio.Semaphore) -> dict:
    """Process a single opportunity: enrich + get messages + AI decision."""
    stage_id = opp.get("pipelineStageId", "")
    stage_name = STAGE_MAP.get(stage_id, stage_id)
    opp_status = opp.get("status", "")
    contact = opp.get("contact", {})
    name = contact.get("name", "Unknown")

    # Quick skip for terminal/complex stages
    if stage_name in ("Leased / Won", "Lost", "Application Sent") or opp_status == "lost":
        return {
            "id": opp["id"], "contact_id": opp.get("contactId", ""),
            "name": name, "phone": contact.get("phone", ""),
            "stage": stage_name, "status": opp_status,
            "property_address": opp.get("name", ""),
            "action": "skip", "message": "", "new_stage": "",
            "reasoning": f"Already in {stage_name}",
            "last_message": "", "last_message_date": "",
            "last_message_direction": "", "last_call_date": "",
            "showing_date": "", "id_status": "",
            "days_since_last_activity": None,
            "tags": contact.get("tags", []),
            "approved": False, "executed": False,
            "available_templates": [],
        }

    async with semaphore:
        try:
            lead = await enrich_lead(client, opp)

            # Find last message, last call, and last 6 SMS messages
            last_msg, last_msg_date, last_msg_dir, last_call_date = "", "", "", ""
            recent_sms = []  # last 6 SMS for review panel
            convos = await client.get(
                f"{GHL_API_BASE}/conversations/search",
                headers=ghl_headers(),
                params={"locationId": GHL_LOCATION_ID, "contactId": lead["contact_id"], "limit": 1},
            )
            convos_data = convos.json() if convos.status_code == 200 else {}
            for conv in convos_data.get("conversations", []):
                msgs_resp = await client.get(
                    f"{GHL_API_BASE}/conversations/{conv['id']}/messages",
                    headers=ghl_headers(), params={"limit": 20},
                )
                if msgs_resp.status_code == 200:
                    for m in msgs_resp.json().get("messages", {}).get("messages", []):
                        mtype = m.get("messageType", "")
                        if mtype in ("TYPE_SMS", "TYPE_EMAIL"):
                            if not last_msg:
                                last_msg = m.get("body", "")[:200]
                                last_msg_date = m.get("dateAdded", "")[:16]
                                last_msg_dir = m.get("direction", "")
                            if len(recent_sms) < 6:
                                recent_sms.append({
                                    "body": m.get("body", "")[:300],
                                    "direction": m.get("direction", ""),
                                    "date": m.get("dateAdded", "")[:16],
                                    "source": m.get("source", ""),
                                })
                        if mtype == "TYPE_CALL" and not last_call_date:
                            last_call_date = m.get("dateAdded", "")[:16]

            days_inactive = None
            if last_msg_date:
                try:
                    last_dt = datetime.fromisoformat(last_msg_date.replace("Z", "+00:00"))
                    days_inactive = (datetime.now(timezone.utc) - last_dt).days
                except (ValueError, TypeError):
                    pass

            if lead["dnd"]:
                decision = {"action": "skip", "reasoning": "DND enabled", "message": "", "new_stage": ""}
            else:
                # Add delay to avoid rate limiting
                await asyncio.sleep(RATE_LIMIT_DELAY)
                decision = await ask_claude(client, lead)


            templates = get_templates_for_stage(stage_name)

            return {
                "id": opp["id"], "contact_id": lead["contact_id"],
                "name": lead["name"], "phone": lead["phone"],
                "email": lead.get("email", ""),
                "stage": stage_name, "status": opp_status,
                "property_address": lead.get("property_address", ""),
                "property_summary": lead.get("property_summary", "")[:100],
                "special_offer": lead.get("special_offer", ""),
                "action": decision.get("action", "skip"),
                "message": decision.get("message", ""),
                "new_stage": decision.get("new_stage", ""),
                "reasoning": decision.get("reasoning", ""),
                "last_message": last_msg, "last_message_date": last_msg_date,
                "last_message_direction": last_msg_dir,
                "last_call_date": last_call_date,
                "showing_date": lead.get("showing_date", ""),
                "id_status": lead.get("id_status", ""),
                "lock_code": lead.get("lock_code", ""),
                "tags": lead.get("tags", []),
                "days_since_last_activity": days_inactive,
                "recent_sms": recent_sms,
                "approved": False, "executed": False,
                "available_templates": [{"id": t["id"], "name": t["name"], "category": t["category"]} for t in templates],
            }
        except Exception as e:
            return {
                "id": opp["id"], "contact_id": opp.get("contactId", ""),
                "name": name, "phone": contact.get("phone", ""),
                "stage": stage_name, "status": opp_status,
                "property_address": "", "action": "error",
                "message": "", "new_stage": "", "reasoning": str(e)[:200],
                "last_message": "", "last_message_date": "",
                "last_message_direction": "", "last_call_date": "",
                "showing_date": "", "id_status": "",
                "days_since_last_activity": None, "tags": [],
                "approved": False, "executed": False, "available_templates": [],
            }


@app.post("/api/scan")
async def api_scan():
    if scan_cache["scanning"]:
        return JSONResponse({"status": "already_scanning"})
    scan_cache["scanning"] = True
    scan_cache["leads"] = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            opps = await get_all_opportunities(client)
            semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
            tasks = [_process_one_opp(client, opp, semaphore) for opp in opps]
            results = await asyncio.gather(*tasks)
            scan_cache["leads"] = list(results)
            scan_cache["scan_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        scan_cache["scanning"] = False

    return JSONResponse({"status": "done", "count": len(scan_cache["leads"])})


@app.get("/api/leads")
async def api_leads():
    return JSONResponse({
        "leads": scan_cache["leads"],
        "scan_time": scan_cache["scan_time"],
        "scanning": scan_cache["scanning"],
    })


@app.post("/api/approve/{opp_id}")
async def api_approve(opp_id: str):
    for lead in scan_cache["leads"]:
        if lead["id"] == opp_id:
            lead["approved"] = not lead["approved"]
            return JSONResponse({"approved": lead["approved"]})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/approve-all")
async def api_approve_all():
    count = 0
    for lead in scan_cache["leads"]:
        if lead["action"] not in ("skip", "error") and not lead["executed"]:
            lead["approved"] = True
            count += 1
    return JSONResponse({"approved_count": count})


@app.post("/api/update-message/{opp_id}")
async def api_update_message(opp_id: str, body: dict):
    """Update the message for a lead (manual edit or template select)."""
    for lead in scan_cache["leads"]:
        if lead["id"] == opp_id:
            lead["message"] = body.get("message", lead["message"])
            if body.get("action"):
                lead["action"] = body["action"]
            return JSONResponse({"ok": True})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/apply-template/{opp_id}/{template_id}")
async def api_apply_template(opp_id: str, template_id: str):
    """Apply a message template to a lead."""
    for lead in scan_cache["leads"]:
        if lead["id"] == opp_id:
            msg = format_template(template_id, lead)
            if msg:
                lead["message"] = msg
                if lead["action"] == "skip":
                    lead["action"] = "send_sms"
                return JSONResponse({"message": msg})
            return JSONResponse({"error": "template not found"}, status_code=404)
    return JSONResponse({"error": "lead not found"}, status_code=404)


@app.post("/api/trigger-voice-bot/{contact_id}")
async def api_call_for_showing(contact_id: str):
    """Trigger the voice AI bot for a contact to schedule showing."""
    # GHL voice AI agents are triggered via workflows
    # For now, return info about how to set this up
    return JSONResponse({
        "status": "pending_setup",
        "agent_id": VOICE_BOT_AGENT_ID,
        "message": "Voice bot trigger needs workflow connection. Create a workflow with 'Contact Tag Added' trigger for tag 'call_for_showing', then add Voice AI action.",
        "contact_id": contact_id,
    })


@app.post("/api/execute")
async def api_execute():
    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for lead in scan_cache["leads"]:
            if not lead["approved"] or lead["executed"]:
                continue
            try:
                action = lead["action"]
                if action in ("send_sms", "send_sms_and_update_stage"):
                    if lead["message"]:
                        await send_sms(client, lead["contact_id"], lead["message"])
                if action in ("update_stage", "send_sms_and_update_stage"):
                    if lead["new_stage"]:
                        await update_stage(client, lead["id"], lead["new_stage"])
                if action == "call_for_showing":
                    await add_contact_tag(client, lead["contact_id"], "call_for_showing")
                lead["executed"] = True
                results.append({"id": lead["id"], "name": lead["name"], "status": "success"})
            except Exception as e:
                results.append({"id": lead["id"], "name": lead["name"], "status": f"error: {e}"})
    return JSONResponse({"results": results})


# ── Scheduler & Webhook endpoints ────────────────────────────────────────────


@app.on_event("startup")
async def startup_event():
    """Start the periodic scheduler on server boot."""
    scheduler.start()


@app.get("/api/scheduler")
async def api_scheduler_status():
    """Get scheduler status."""
    return JSONResponse({
        "running": scheduler.running,
        "dry_run": scheduler.dry_run,
        "interval_seconds": 10800,
        "last_run": scheduler.last_run,
        "last_result_summary": {
            "actions": scheduler.last_result.get("actions", 0),
            "skipped": scheduler.last_result.get("skipped", 0),
            "errors": scheduler.last_result.get("errors", 0),
        } if scheduler.last_result else None,
        "business_hours": is_business_hours(),
    })


@app.post("/api/scheduler/toggle-live")
async def api_scheduler_toggle():
    """Toggle scheduler between dry-run and live mode."""
    if scheduler.dry_run:
        scheduler.set_live()
    else:
        scheduler.set_dry_run()
    return JSONResponse({"dry_run": scheduler.dry_run})


@app.post("/api/webhook/inbound")
async def api_webhook_inbound(request: Request):
    """Webhook for GHL to call on inbound messages.
    Expects: {"contact_id": "xxx", "message": "optional body"}

    GHL Workflow setup:
    1. Trigger: Customer Reply / Inbound Message
    2. Action: Webhook → POST http://YOUR_SERVER:8000/api/webhook/inbound
    3. Body: {"contact_id": "{{contact.id}}", "message": "{{message.body}}"}
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        return JSONResponse({"error": f"Invalid JSON: {str(e)[:100]}"}, status_code=400)

    # Log the raw payload first so we can see exactly what GHL sends
    logger.info(f"Webhook raw payload: {json.dumps(body)[:500]}")

    contact_id = (
        body.get("contact_id")
        or body.get("contactId")
        or body.get("contact", {}).get("id", "")
        if isinstance(body, dict) else ""
    )

    # GHL may send message as a string, dict, or nested object — extract safely
    raw_message = body.get("message", "") if isinstance(body, dict) else ""
    if isinstance(raw_message, dict):
        # e.g. {"body": "Hello", "type": "SMS"}
        message = raw_message.get("body") or raw_message.get("text") or str(raw_message)
    else:
        message = str(raw_message) if raw_message is not None else ""

    # Log the webhook call
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "contact_id": contact_id,
        "message": message[:100],
        "status": "received"
    }

    if not contact_id:
        log_entry["status"] = "error: no contact_id"
        webhook_log.append(log_entry)
        logger.warning(f"Webhook received with no contact_id. Body: {body}")
        return JSONResponse({"error": "contact_id required"}, status_code=400)

    logger.info(f"Webhook received for contact {contact_id}, message: {message[:50]}")

    # Use scheduler's dry_run setting
    try:
        result = await handle_inbound(contact_id, message, dry_run=scheduler.dry_run)
        logger.info(f"handle_inbound returned: {result}")
        log_entry["status"] = result.get("status", "processed")
        log_entry["action"] = result.get("action", "skip")
        log_entry["bot_message"] = result.get("message", "")
        log_entry["bot_follow_up"] = result.get("follow_up_message", "")
        log_entry["lead_name"] = result.get("name", "")
        log_entry["stage"] = result.get("stage", "")
        log_entry["property"] = result.get("property_address", "")
    except Exception as e:
        logger.exception(f"Error in handle_inbound: {e}")
        log_entry["status"] = f"error: {str(e)[:50]}"
        result = {"status": "error", "message": str(e)[:200]}

    webhook_log.append(log_entry)
    # Keep only last 100 webhook logs
    if len(webhook_log) > 100:
        webhook_log.pop(0)

    return JSONResponse(result)


@app.get("/api/redirect-data")
async def api_redirect_data(c: str = ""):
    """Return contact details for the tpmd.io/go redirect page."""
    if not c:
        return JSONResponse({"error": "contact_id required"}, status_code=400)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{GHL_API_BASE}/contacts/{c}",
                headers={"Authorization": f"Bearer {GHL_API_KEY}", "Version": "2021-07-28", "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return JSONResponse({"error": "contact not found"}, status_code=404)
            contact = resp.json().get("contact", {})
            custom = {f["id"]: f["value"] for f in contact.get("customFields", []) if f.get("value")}
            property_address = ""
            for field_id, value in custom.items():
                if field_id == "Vk9hcLmQAaoeLYYPbUUe":
                    property_address = value
                    break
            return JSONResponse({
                "first_name": contact.get("firstName", ""),
                "last_name": contact.get("lastName", ""),
                "email": contact.get("email", ""),
                "phone": contact.get("phone", ""),
                "property_address": property_address,
            }, headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/webhook-log")
async def api_webhook_log():
    """Get recent webhook activity for debugging."""
    return JSONResponse({
        "total_received": len(webhook_log),
        "recent": webhook_log[-20:]  # Last 20 entries
    })


@app.post("/api/test-chat")
async def api_test_chat(request: Request):
    """Simulate bot response for testing — no real SMS sent."""
    try:
        body = await request.json()
        message = body.get("message", "")
        stage = body.get("stage", "New Lead")
        name = body.get("name", "Test Lead")
        property_address = body.get("property_address", "")
        property_summary = body.get("property_summary", "")
        lock_code = body.get("lock_code", "")
        backup_lock_code = body.get("backup_lock_code", "")
        history = body.get("history", [])  # [{role:"lead"|"bot", text:"..."}]

        now_iso = datetime.now(timezone.utc).isoformat()

        # Build recent_messages from history + current inbound message
        # ask_claude expects newest first
        recent_messages = [{
            "direction": "inbound",
            "body": message,
            "date": now_iso[:16],
            "type": "TYPE_SMS",
            "source": "",
        }]
        for entry in history[-5:]:
            direction = "inbound" if entry["role"] == "lead" else "outbound"
            source = "bot" if entry["role"] == "bot" else ""
            recent_messages.append({
                "direction": direction,
                "body": entry["text"],
                "date": now_iso[:16],
                "type": "TYPE_SMS",
                "source": source,
            })

        fake_lead = {
            "opportunity_id": "test_opp",
            "contact_id": "test_contact",
            "name": name,
            "phone": "+15551234567",
            "email": "",
            "tags": [],
            "dnd": False,
            "stage": stage,
            "stage_id": "",
            "opp_status": "open",
            "opp_name": property_address or "Test Property",
            "created_at": now_iso,
            "last_stage_change": now_iso,
            "property_address": property_address,
            "id_status": "verified",
            "lock_code": lock_code,
            "backup_lock_code": backup_lock_code,
            "showing_date": "",
            "showing_time": "",
            "application_url": "",
            "special_offer": "",
            "property_summary": property_summary,
            "property_full_listing": property_summary,
            "property_headline": "",
            "ai_summary": "",
            "recent_messages": recent_messages[:6],
            "current_time": now_iso,
            "id_verification_url": "https://example.com/schedule",
            "reschedule_url": "https://example.com/reschedule",
            "access_code_url": "",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            decision = await ask_claude(client, fake_lead)

        return JSONResponse({
            "action": decision.get("action", "skip"),
            "message": decision.get("message", ""),
            "follow_up_message": decision.get("follow_up_message", ""),
            "reasoning": decision.get("reasoning", ""),
            "new_stage": decision.get("new_stage", ""),
        })
    except Exception as e:
        logger.error(f"test-chat error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


BOT_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_rules.txt")
# GHL Custom Value key used to persist bot rules across deploys
BOT_RULES_GHL_KEY = "bot_rules"

async def _ghl_get_bot_rules(client: httpx.AsyncClient) -> str:
    """Read bot rules from GHL Custom Values (persists across Railway deploys)."""
    try:
        resp = await client.get(
            f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
            headers=ghl_headers(),
        )
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == BOT_RULES_GHL_KEY:
                    return cv.get("value", "")
    except Exception as e:
        logger.warning(f"Could not read bot rules from GHL: {e}")
    # Fallback to local file
    try:
        with open(BOT_RULES_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


async def _ghl_set_bot_rules(client: httpx.AsyncClient, rules: str) -> bool:
    """Upsert bot rules in GHL Custom Values."""
    try:
        # Get existing custom values to find the ID (if exists)
        resp = await client.get(
            f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
            headers=ghl_headers(),
        )
        cv_id = None
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == BOT_RULES_GHL_KEY:
                    cv_id = cv.get("id")
                    break

        if cv_id:
            r = await client.put(
                f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues/{cv_id}",
                headers=ghl_headers(),
                json={"name": BOT_RULES_GHL_KEY, "value": rules},
            )
        else:
            r = await client.post(
                f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
                headers=ghl_headers(),
                json={"name": BOT_RULES_GHL_KEY, "value": rules},
            )
        return r.status_code in (200, 201)
    except Exception as e:
        logger.warning(f"Could not save bot rules to GHL: {e}")

    # Fallback: save to local file
    try:
        with open(BOT_RULES_PATH, "w", encoding="utf-8") as f:
            f.write(rules)
        return True
    except Exception:
        return False


@app.get("/api/stale-leads")
async def api_stale_leads_preview():
    """Preview leads with no inbound activity in 10+ days, excluding Application Sent and future showings."""
    import asyncio as _aio
    from datetime import datetime as dt, timezone, timedelta
    cutoff = dt.now(timezone.utc) - timedelta(days=10)
    now_iso = dt.now(timezone.utc).isoformat()
    EXEMPT_STAGES = {"Application Sent", "Leased / Won", "Lost", "Showing Scheduled"}
    CALENDAR_ID = "I27t4Z2T7ZG0SQlI3Syd"

    async def _last_inbound(client, contact_id):
        """Return last inbound message dateAdded, or None."""
        try:
            convos = await ghl_get(client, "/conversations/search", {
                "locationId": GHL_LOCATION_ID, "contactId": contact_id, "limit": 1
            })
            for conv in convos.get("conversations", []):
                msgs = await ghl_get(client, f"/conversations/{conv['id']}/messages", {"limit": 50})
                for m in (msgs.get("messages") or {}).get("messages", []):
                    if m.get("direction") == "inbound":
                        return m.get("dateAdded", "")
        except Exception:
            pass
        return None

    async def _has_future_showing(client, contact_id):
        try:
            appts = await ghl_get(client, f"/contacts/{contact_id}/appointments")
            for appt in (appts or {}).get("events", []):
                if appt.get("calendarId") == CALENDAR_ID and appt.get("startTime", "") > now_iso:
                    return True
        except Exception:
            pass
        return False

    async with httpx.AsyncClient(timeout=60) as client:
        opps = await get_all_opportunities(client)

        # Filter to non-exempt candidates
        candidates = [
            o for o in opps
            if STAGE_MAP.get(o.get("pipelineStageId", ""), "") not in EXEMPT_STAGES
            and o.get("status") != "lost"
        ]

        stale = []
        # Process in batches of 5 to avoid GHL rate limits
        for i in range(0, len(candidates), 5):
            batch = candidates[i:i+5]
            for opp in batch:
                stage_name = STAGE_MAP.get(opp.get("pipelineStageId", ""), "")
                contact_id = opp.get("contactId", "")
                name = (opp.get("contact") or {}).get("name", contact_id)
                created = opp.get("createdAt", "")

                # Check appointments first — skip if future showing booked
                if await _has_future_showing(client, contact_id):
                    continue

                # Find last inbound message
                last_inbound = await _last_inbound(client, contact_id)

                if not last_inbound:
                    # Never replied — stale if created > 10 days ago
                    if created:
                        created_dt = dt.fromisoformat(created.replace("Z", "+00:00"))
                        if created_dt < cutoff:
                            stale.append({"opp_id": opp["id"], "contact_id": contact_id,
                                          "name": name, "stage": stage_name,
                                          "last_inbound": "Never", "created": created[:10]})
                else:
                    last_dt = dt.fromisoformat(last_inbound.replace("Z", "+00:00"))
                    if last_dt < cutoff:
                        stale.append({"opp_id": opp["id"], "contact_id": contact_id,
                                      "name": name, "stage": stage_name,
                                      "last_inbound": last_inbound[:10], "created": created[:10]})

    return JSONResponse({"stale": stale, "count": len(stale)})


@app.post("/api/stale-leads/archive")
async def api_stale_leads_archive(request: Request):
    """Move provided opportunity IDs to Lost."""
    body = await request.json()
    opp_ids = body.get("opp_ids", [])
    results = {"moved": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=30) as client:
        for opp_id in opp_ids:
            try:
                await update_stage(client, opp_id, "Lost")
                results["moved"] += 1
            except Exception as e:
                logger.error(f"Failed to archive {opp_id}: {e}")
                results["errors"] += 1
    return JSONResponse(results)


async def _ghl_upsert_cv(client: httpx.AsyncClient, key: str, value: str):
    """Upsert a GHL Custom Value by name."""
    resp = await client.get(f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues", headers=ghl_headers())
    cv_id = None
    if resp.status_code == 200:
        for cv in resp.json().get("customValues", []):
            if cv.get("name") == key:
                cv_id = cv.get("id"); break
    if cv_id:
        await client.put(f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues/{cv_id}", headers=ghl_headers(), json={"name": key, "value": value})
    else:
        await client.post(f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues", headers=ghl_headers(), json={"name": key, "value": value})


async def _ghl_get_cv(client: httpx.AsyncClient, key: str) -> str:
    """Read a GHL Custom Value by name."""
    try:
        resp = await client.get(f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues", headers=ghl_headers())
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == key:
                    return cv.get("value", "")
    except Exception:
        pass
    return ""


@app.get("/api/properties")
async def api_get_properties():
    """Get all properties with availability status."""
    async with httpx.AsyncClient(timeout=10) as client:
        raw = await _ghl_get_cv(client, "properties_list")
    try:
        props = json.loads(raw) if raw else []
    except Exception:
        props = []
    return JSONResponse({"properties": props})


@app.post("/api/properties")
async def api_set_properties(request: Request):
    """Save properties list. Each item: {address, available, notes}"""
    try:
        body = await request.json()
        props = body.get("properties", [])
        raw = json.dumps(props, ensure_ascii=False)
        # Also update old unavailable_properties key for backward compat with lease_agent
        unavailable_text = "\n".join(p["address"] for p in props if not p.get("available", True))
        async with httpx.AsyncClient(timeout=10) as client:
            await _ghl_upsert_cv(client, "properties_list", raw)
            await _ghl_upsert_cv(client, "unavailable_properties", unavailable_text)
        return JSONResponse({"status": "saved", "count": len(props)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/bot-rules")
async def api_get_bot_rules():
    async with httpx.AsyncClient(timeout=10) as client:
        rules = await _ghl_get_bot_rules(client)
    return JSONResponse({"rules": rules})

@app.post("/api/bot-rules")
async def api_set_bot_rules(request: Request):
    try:
        body = await request.json()
        rules = body.get("rules", "")
        async with httpx.AsyncClient(timeout=10) as client:
            ok = await _ghl_set_bot_rules(client, rules)
        logger.info(f"Bot rules updated: {len(rules)} chars, ghl_saved={ok}")
        return JSONResponse({"status": "saved", "chars": len(rules)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/lead-messages/{contact_id}")
async def api_lead_messages(contact_id: str):
    """Fetch last 6 SMS messages for a contact directly from GHL."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            convos_resp = await client.get(
                f"{GHL_API_BASE}/conversations/search",
                headers=ghl_headers(),
                params={"locationId": GHL_LOCATION_ID, "contactId": contact_id, "limit": 1},
            )
            if convos_resp.status_code != 200:
                return JSONResponse({"messages": []})
            conversations = convos_resp.json().get("conversations", [])
            if not conversations:
                return JSONResponse({"messages": []})
            conv_id = conversations[0]["id"]
            msgs_resp = await client.get(
                f"{GHL_API_BASE}/conversations/{conv_id}/messages",
                headers=ghl_headers(),
                params={"limit": 30},
            )
            if msgs_resp.status_code != 200:
                return JSONResponse({"messages": []})
            all_msgs = msgs_resp.json().get("messages", {}).get("messages", [])
            sms_msgs = []
            for m in all_msgs:
                if m.get("messageType") in ("TYPE_SMS", "TYPE_EMAIL"):
                    sms_msgs.append({
                        "body": m.get("body", "")[:500],
                        "direction": m.get("direction", ""),
                        "date": m.get("dateAdded", "")[:16],
                        "source": m.get("source", ""),
                    })
                    if len(sms_msgs) >= 6:
                        break
            return JSONResponse({"messages": sms_msgs})
    except Exception as e:
        return JSONResponse({"messages": [], "error": str(e)})


@app.post("/api/bot-feedback")
async def api_bot_feedback(request: Request):
    """Append message feedback as a rule (persisted to GHL)."""
    try:
        body = await request.json()
        inbound = body.get("inbound", "")
        bot_msg = body.get("bot_message", "")
        feedback = body.get("feedback", "")
        rating = body.get("rating", "bad")

        if rating == "bad" and feedback:
            from datetime import datetime as dt
            timestamp = dt.now().strftime("%Y-%m-%d %H:%M")
            rule_line = f"\n# Feedback [{timestamp}] — Lead said: \"{inbound[:80]}\" → Bot replied: \"{bot_msg[:80]}\"\n# Issue: {feedback}\n{feedback}\n"
            async with httpx.AsyncClient(timeout=10) as client:
                existing = await _ghl_get_bot_rules(client)
                await _ghl_set_bot_rules(client, existing + rule_line)
            logger.info(f"Feedback rule appended: {feedback[:80]}")
            return JSONResponse({"status": "saved"})
        return JSONResponse({"status": "skipped"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/vapi-webhook")
async def api_vapi_webhook(request: Request):
    """Receive VAPI end-of-call report, write AI Summary to GHL, update pipeline stage."""
    import json as _json
    from lease_agent import CALL_LOG_FILE, parse_custom_fields, ghl_post, ghl_get, CUSTOM_FIELDS, STAGE_NAME_TO_ID
    try:
        body = await request.json()
        msg_type = body.get("type", "")
        if msg_type not in ("end-of-call-report", "call.ended"):
            return JSONResponse({"status": "ignored", "type": msg_type})

        summary = body.get("summary", "") or body.get("call", {}).get("summary", "")
        customer_phone = (
            body.get("customer", {}).get("number", "") or
            body.get("call", {}).get("customer", {}).get("number", "")
        )
        call_started_at = body.get("startedAt", "") or body.get("call", {}).get("startedAt", "")
        ended_reason = body.get("endedReason", "") or body.get("call", {}).get("endedReason", "")

        if not customer_phone:
            return JSONResponse({"status": "no_phone"})

        # Normalize phone: ensure +1 prefix
        phone = customer_phone.strip()
        if not phone.startswith("+"):
            phone = "+" + phone

        # Determine call outcome for stage update
        # call_started = False means the call never connected (error before dial)
        call_connected = not ended_reason.startswith("call.start.error") if ended_reason else bool(call_started_at)
        # "Answered" = call connected AND not immediately silent/voicemail
        no_answer_reasons = {"customer-did-not-answer", "silence-timed-out", "voicemail"}
        lead_answered = call_connected and ended_reason not in no_answer_reasons

        async with httpx.AsyncClient(timeout=20) as client:
            # Find contact by phone
            search = await ghl_get(client, "/contacts/search/duplicate", {
                "locationId": GHL_LOCATION_ID, "phone": phone
            })
            contact = search.get("contact", None)
            if not contact:
                # fallback: search by query
                contacts_resp = await ghl_get(client, "/contacts/", {
                    "locationId": GHL_LOCATION_ID, "query": phone, "limit": 1
                })
                contacts = contacts_resp.get("contacts", [])
                contact = contacts[0] if contacts else None

            if not contact:
                logger.warning(f"VAPI webhook: no contact found for {phone}")
                return JSONResponse({"status": "contact_not_found", "phone": phone})

            contact_id = contact.get("id", "")
            name = contact.get("name", "")

            # Find AI Summary field ID
            ai_summary_field_id = None
            for fid, fname in CUSTOM_FIELDS.items():
                if fname == "ai_summary":
                    ai_summary_field_id = fid
                    break
            if not ai_summary_field_id:
                for cf in contact.get("customFields", []):
                    fkey = (cf.get("fieldKey") or cf.get("name") or "").lower().replace(" ", "_")
                    if "ai_summary" in fkey:
                        ai_summary_field_id = cf.get("id", "")
                        break

            if summary and ai_summary_field_id:
                await ghl_post(client, f"/contacts/{contact_id}", {
                    "customFields": [{"id": ai_summary_field_id, "field_value": summary}]
                })

            # ── Update pipeline stage based on call outcome ───────────────────
            stage_updated = None
            if call_connected:
                target_stage = "Call: Answered" if lead_answered else "Call: No Answer"
                target_stage_id = STAGE_NAME_TO_ID.get(target_stage)
                if target_stage_id:
                    # Find their opportunity
                    try:
                        opps_resp = await client.get(
                            f"{GHL_API_BASE}/opportunities/search",
                            headers=ghl_headers(),
                            params={
                                "location_id": GHL_LOCATION_ID,
                                "pipeline_id": "DVv60aGSOc7XtIofy4Pn",
                                "contact_id": contact_id,
                                "limit": 1,
                            },
                        )
                        if opps_resp.status_code == 200:
                            opps_data = opps_resp.json().get("opportunities", [])
                            if opps_data:
                                opp_id = opps_data[0].get("id", "")
                                current_stage = STAGE_MAP.get(opps_data[0].get("pipelineStageId", ""), "")
                                # Only update if not in a terminal / later stage
                                terminal = {"Leased / Won", "Lost", "Application Sent", "Showing Scheduled", "Call: Answered"}
                                if current_stage not in terminal or (target_stage == "Call: Answered" and current_stage == "Call: No Answer"):
                                    patch = await client.patch(
                                        f"{GHL_API_BASE}/opportunities/{opp_id}",
                                        headers=ghl_headers(),
                                        json={"pipelineStageId": target_stage_id},
                                    )
                                    if patch.status_code in (200, 201):
                                        stage_updated = target_stage
                                        logger.info(f"VAPI webhook: moved {name} → {target_stage}")
                                    else:
                                        logger.warning(f"VAPI webhook: stage update failed {patch.status_code}: {patch.text[:100]}")
                    except Exception as stage_err:
                        logger.warning(f"VAPI webhook: stage update error: {stage_err}")

            logger.info(f"VAPI webhook: processed {name} ({contact_id}), answered={lead_answered}, stage_updated={stage_updated}")
            return JSONResponse({
                "status": "ok",
                "contact_id": contact_id,
                "name": name,
                "call_connected": call_connected,
                "lead_answered": lead_answered,
                "stage_updated": stage_updated,
            })

    except Exception as e:
        logger.error(f"VAPI webhook error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/call-log")
async def api_call_log():
    """Return call log — live from VAPI API (always fresh, no file dependency)."""
    import json as _json

    SO_CALL_SUMMARY_ID    = "db31e82f-c498-443f-9e08-5cd2e43b3113"
    SO_SUCCESS_EVAL_ID    = "d45f09c9-847c-4bd5-8cd5-7ba63b3df883"
    SO_APPT_BOOKED_ID     = "4030f8dc-4f0c-452c-a422-b6e5157e0c68"
    SO_APPT_CANCELLED_ID  = "5da34a47-1843-4a3a-8e1e-fc69d92f3cb5"
    SO_APPT_RESCHEDULED_ID= "8708977d-98ea-4f4b-ab96-a6ef4a719a20"
    SO_APPT_DATETIME_ID   = "220485af-d137-4df7-827b-54814e604322"

    def _parse_outcome(so):
        if not so: return ""
        booked      = so.get(SO_APPT_BOOKED_ID, {}).get("result")
        cancelled   = so.get(SO_APPT_CANCELLED_ID, {}).get("result")
        rescheduled = so.get(SO_APPT_RESCHEDULED_ID, {}).get("result")
        success     = so.get(SO_SUCCESS_EVAL_ID, {}).get("result")
        appt_dt     = so.get(SO_APPT_DATETIME_ID, {}).get("result", "")
        if booked is True:      return f"BOOKED: {appt_dt}" if appt_dt else "BOOKED"
        if cancelled is True:   return "CANCELLED"
        if rescheduled is True: return f"RESCHEDULED: {appt_dt}" if appt_dt else "RESCHEDULED"
        if success is True:     return "SUCCESS"
        if success is False:    return "NOT_SUCCESS"
        return ""

    # Fetch all pages from VAPI
    raw_entries = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page, limit = 1, 100
            while True:
                resp = await client.get(
                    f"{VAPI_BASE}/v2/call",
                    headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
                    params={"page": page, "limit": limit, "phoneNumberId": VAPI_PHONE_NUMBER_ID},
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("results", [])
                if not batch: break
                for call in batch:
                    phone = (call.get("customer") or {}).get("number", "")
                    if phone.replace(" ","").replace("-","") in {"+18132145068","18132145068","8132145068"}:
                        continue
                    ended_reason = call.get("endedReason", "")
                    started_at   = call.get("startedAt", "") or call.get("createdAt", "")
                    ended_at     = call.get("endedAt", "")
                    created_at   = call.get("createdAt", "")
                    call_started = "NO" if ended_reason.startswith("call.start.error") else "YES"
                    duration_seconds = 0
                    if call_started == "YES" and started_at and ended_at:
                        try:
                            from datetime import datetime as _dt
                            s = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                            e = _dt.fromisoformat(ended_at.replace("Z", "+00:00"))
                            duration_seconds = max(0, int((e - s).total_seconds()))
                        except Exception:
                            pass
                    vv      = (call.get("assistantOverrides") or {}).get("variableValues") or {}
                    so      = (call.get("artifact") or {}).get("structuredOutputs") or {}
                    summary = (so.get(SO_CALL_SUMMARY_ID) or {}).get("result", "")
                    outcome = _parse_outcome(so)
                    name    = vv.get("name", "") or phone
                    raw_entries.append({
                        "vapi_call_id":     call.get("id", ""),
                        "name":             name,
                        "phone":            phone,
                        "property_address": vv.get("property_address", ""),
                        "triggered_at":     created_at,
                        "started_at":       started_at,
                        "call_started":     call_started,
                        "duration_seconds": duration_seconds,
                        "ended_reason":     ended_reason,
                        "ai_summary":       summary,
                        "outcome":          outcome,
                    })
                meta = data.get("metadata", {})
                total = meta.get("totalItems", 0)
                per   = meta.get("itemsPerPage", limit)
                cur   = meta.get("currentPage", page)
                if cur * per >= total: break
                page += 1
    except Exception as ex:
        raw_entries = []

    def _to_et(ts):
        if not ts:
            return "", ""
        try:
            eastern = timezone(timedelta(hours=-4))
            dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt_et = dt_utc.astimezone(eastern)
            return dt_et.strftime("%Y-%m-%d"), dt_et.strftime("%H:%M")
        except Exception:
            return ts[:10], ts[11:16]

    def _outcome_label(e):
        outcome = e.get("outcome", "")
        call_started = e.get("call_started", "YES")
        ended_reason = e.get("ended_reason", "")
        ai_summary = e.get("ai_summary", "")

        if call_started == "NO":
            if "outbound-daily-limit" in ended_reason:
                return "Daily limit reached", "#f97316"
            return "Did not connect", "#94a3b8"
        if outcome.startswith("BOOKED"):
            return outcome, "#22c55e"
        if outcome == "CANCELLED":
            return "Cancelled", "#ef4444"
        if outcome.startswith("RESCHEDULED"):
            return outcome, "#4C6EF5"
        if outcome == "SUCCESS":
            return "Success", "#22c55e"
        if outcome == "NOT_SUCCESS":
            sl = ai_summary.lower()
            if any(w in sl for w in ["voicemail", "no answer", "left message", "mailbox"]):
                return "Voicemail", "#f59e0b"
            if any(w in sl for w in ["not interested", "no longer", "do not call", "wrong number"]):
                return "Not interested", "#ef4444"
            if "full" in sl and "mailbox" in sl:
                return "Mailbox full", "#f59e0b"
            return "Not successful", "#f59e0b"
        if ended_reason == "silence-timed-out":
            return "Voicemail / No answer", "#f59e0b"
        if ended_reason == "customer-did-not-answer":
            return "No answer", "#94a3b8"
        if ai_summary:
            return "Spoke — see summary", "#4C6EF5"
        return "No summary", "#94a3b8"

    TEST_NUMBERS = {"+18132145068", "18132145068", "8132145068"}

    entries = []
    for e in raw_entries:
        if e.get("phone", "").replace(" ", "").replace("-", "") in TEST_NUMBERS:
            continue
        call_date, call_time = _to_et(e.get("triggered_at", "") or e.get("started_at", ""))
        result_label, result_color = _outcome_label(e)
        dur = e.get("duration_seconds", 0)
        dur_str = f"{dur}s" if dur else "—"
        entries.append({
            "name": e.get("name", "") or e.get("phone", ""),
            "phone": e.get("phone", ""),
            "stage": e.get("stage", ""),
            "property_address": e.get("property_address", ""),
            "call_date": call_date,
            "call_time": call_time,
            "call_started": e.get("call_started", "YES"),
            "duration": dur_str,
            "ai_summary": e.get("ai_summary", ""),
            "outcome": e.get("outcome", ""),
            "result_label": result_label,
            "result_color": result_color,
        })

    entries.sort(key=lambda x: x.get("call_date", ""), reverse=True)
    return JSONResponse({"calls": entries, "total": len(entries)})


@app.get("/api/call-center")
async def api_call_center():
    """Return eligible leads enriched with call history — fetches live from VAPI API."""
    import zoneinfo as _zi

    ELIGIBLE_STAGES = {
        "Verification Auto-Sent", "ID Verified", "ID Rejected",
        "Call: No Answer", "Call: Answered",
    }

    SO_CALL_SUMMARY_ID    = "db31e82f-c498-443f-9e08-5cd2e43b3113"
    SO_SUCCESS_EVAL_ID    = "d45f09c9-847c-4bd5-8cd5-7ba63b3df883"
    SO_APPT_BOOKED_ID     = "4030f8dc-4f0c-452c-a422-b6e5157e0c68"
    SO_APPT_CANCELLED_ID  = "5da34a47-1843-4a3a-8e1e-fc69d92f3cb5"
    SO_APPT_RESCHEDULED_ID= "8708977d-98ea-4f4b-ab96-a6ef4a719a20"

    eastern = _zi.ZoneInfo("America/New_York")
    TEST_NUMBERS = {"+18132145068", "18132145068", "8132145068"}

    def _norm_phone(p: str) -> str:
        """Normalize to digits-only for matching."""
        return (p or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").lstrip("+")

    def _to_et_date(ts: str):
        if not ts:
            return None
        try:
            dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt_utc.astimezone(eastern).strftime("%Y-%m-%d")
        except Exception:
            return ts[:10] if ts else None

    def _parse_outcome(so):
        if not so: return ""
        booked      = so.get(SO_APPT_BOOKED_ID, {}).get("result")
        cancelled   = so.get(SO_APPT_CANCELLED_ID, {}).get("result")
        rescheduled = so.get(SO_APPT_RESCHEDULED_ID, {}).get("result")
        success     = so.get(SO_SUCCESS_EVAL_ID, {}).get("result")
        if booked is True:      return "BOOKED"
        if cancelled is True:   return "CANCELLED"
        if rescheduled is True: return "RESCHEDULED"
        if success is True:     return "SUCCESS"
        if success is False:    return "NOT_SUCCESS"
        return ""

    def _call_label(call: dict) -> str:
        """Human-readable call status label."""
        outcome = call.get("outcome", "")
        ended_reason = call.get("ended_reason", "")
        call_started = call.get("call_started", "YES")
        summary_lower = (call.get("ai_summary", "") or "").lower()

        if call_started == "NO":
            return "Did not connect"
        if outcome == "BOOKED":
            return "Booked"
        if outcome == "CANCELLED":
            return "Cancelled"
        if outcome == "RESCHEDULED":
            return "Rescheduled"
        if outcome == "SUCCESS":
            return "Success"
        if outcome == "NOT_SUCCESS":
            if any(w in summary_lower for w in ["voicemail", "no answer", "left message", "mailbox"]):
                return "Voicemail"
            if any(w in summary_lower for w in ["not interested", "no longer", "do not call", "wrong number"]):
                return "Not interested"
            return "Not successful"
        if ended_reason in ("silence-timed-out", "customer-did-not-answer"):
            return "Voicemail / No answer"
        if call.get("ai_summary"):
            return "Spoke"
        return "Unknown"

    # ── Fetch all calls from VAPI (live, paginated) ──────────────────────────
    # phone_norm → list of call dicts, sorted newest-first
    phone_calls: dict[str, list] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as vapi_client:
            page, limit = 1, 100
            while True:
                resp = await vapi_client.get(
                    f"{VAPI_BASE}/v2/call",
                    headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
                    params={"page": page, "limit": limit, "phoneNumberId": VAPI_PHONE_NUMBER_ID},
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("results", [])
                if not batch:
                    break
                for call in batch:
                    raw_phone = (call.get("customer") or {}).get("number", "")
                    norm = _norm_phone(raw_phone)
                    if norm in {"18132145068", "8132145068"}:
                        continue
                    ended_reason = call.get("endedReason", "")
                    started_at   = call.get("startedAt", "") or ""
                    created_at   = call.get("createdAt", "") or ""
                    call_started = "NO" if ended_reason.startswith("call.start.error") else "YES"
                    so      = (call.get("artifact") or {}).get("structuredOutputs") or {}
                    summary = (so.get(SO_CALL_SUMMARY_ID) or {}).get("result", "")
                    outcome = _parse_outcome(so)
                    entry = {
                        "vapi_call_id": call.get("id", ""),
                        "triggered_at": created_at,
                        "started_at":   started_at,
                        "call_started": call_started,
                        "ended_reason": ended_reason,
                        "ai_summary":   summary,
                        "outcome":      outcome,
                    }
                    phone_calls.setdefault(norm, []).append(entry)
                meta = data.get("metadata", {})
                total = meta.get("totalItems", 0)
                per   = meta.get("itemsPerPage", limit)
                cur   = meta.get("currentPage", page)
                if cur * per >= total:
                    break
                page += 1
        # Sort each phone's calls newest-first
        for norm in phone_calls:
            phone_calls[norm].sort(
                key=lambda c: c.get("triggered_at") or c.get("started_at") or "",
                reverse=True,
            )
    except Exception as ex:
        logger.warning(f"call-center: VAPI fetch failed: {ex}")
        phone_calls = {}

    # ── Fetch eligible opportunities ─────────────────────────────────────────
    from lease_agent import parse_custom_fields as _parse_cf

    async with httpx.AsyncClient(timeout=30) as client:
        opps = await get_all_opportunities(client)

        # Pre-filter eligible leads, then fetch full contact for each to get custom fields
        eligible_opps = [
            opp for opp in opps
            if STAGE_MAP.get(opp.get("pipelineStageId", ""), opp.get("pipelineStageId", "")) in ELIGIBLE_STAGES
            and opp.get("status") != "lost"
        ]

        # Fetch full contact details for each eligible lead (custom fields incl. property_address)
        contact_custom: dict[str, dict] = {}
        for opp in eligible_opps:
            cid = opp.get("contactId", "")
            if not cid or cid in contact_custom:
                continue
            try:
                cr = await client.get(f"{GHL_API_BASE}/contacts/{cid}", headers=ghl_headers())
                if cr.status_code == 200:
                    cdata = cr.json().get("contact", cr.json())
                    contact_custom[cid] = _parse_cf(cdata.get("customFields", []))
            except Exception:
                contact_custom[cid] = {}

    _today_et = datetime.now(eastern).strftime("%Y-%m-%d")
    leads_out = []

    for opp in eligible_opps:
        stage_id = opp.get("pipelineStageId", "")
        stage_name = STAGE_MAP.get(stage_id, stage_id)

        contact = opp.get("contact", {})
        contact_id = opp.get("contactId", "")
        opp_id = opp.get("id", "")
        name = contact.get("name", "Unknown")
        phone = contact.get("phone", "")
        norm_lead = _norm_phone(phone)

        # Match calls by normalized phone (strip leading 1 for US numbers)
        def _match_calls(norm: str):
            # Try exact, then strip/add leading 1
            if norm in phone_calls:
                return phone_calls[norm]
            alt = norm[1:] if norm.startswith("1") and len(norm) == 11 else "1" + norm
            return phone_calls.get(alt, [])

        contact_calls = _match_calls(norm_lead)

        last_call_date = None
        last_call_status = None
        ai_summary = None
        if contact_calls:
            latest = contact_calls[0]
            last_call_date = _to_et_date(latest.get("triggered_at") or latest.get("started_at") or "")
            last_call_status = _call_label(latest)
            for c in contact_calls:
                if c.get("ai_summary"):
                    ai_summary = c["ai_summary"]
                    break

        # ── Skip logic (inline, no call_log.json dependency) ─────────────────
        skip_reason = ""
        should_skip = False

        # 1. Called today?
        called_today = any(
            (_to_et_date(c.get("triggered_at") or c.get("started_at") or "") == _today_et)
            for c in contact_calls
        )
        if called_today:
            should_skip = True
            skip_reason = f"Already called today ({_today_et})"

        # 2. Not interested? (any connected call with not-interested summary)
        if not should_skip:
            for c in contact_calls:
                if c.get("call_started") == "YES" and c.get("ai_summary"):
                    sl = c["ai_summary"].lower()
                    if any(w in sl for w in ["not interested", "do not call", "no longer interested", "wrong number", "stop calling"]):
                        should_skip = True
                        skip_reason = "Lead not interested — do not call"
                        break

        # 3. Voicemail 2+ consecutive times?
        if not should_skip:
            connected = [c for c in contact_calls if c.get("call_started") == "YES"]
            if len(connected) >= 2:
                def _is_voicemail(c):
                    sl = (c.get("ai_summary") or "").lower()
                    er = c.get("ended_reason", "")
                    return (
                        any(w in sl for w in ["voicemail", "no answer", "left message", "mailbox"])
                        or er in ("silence-timed-out", "customer-did-not-answer")
                        or c.get("outcome") == "NOT_SUCCESS"
                    )
                if _is_voicemail(connected[0]) and _is_voicemail(connected[1]):
                    should_skip = True
                    skip_reason = "Voicemail 2x in a row — pausing calls"

        can_call = not should_skip

        # Get property_address from fetched custom fields
        property_address = contact_custom.get(contact_id, {}).get("property_address", "")

        leads_out.append({
            "contact_id": contact_id,
            "opp_id": opp_id,
            "name": name,
            "phone": phone,
            "stage": stage_name,
            "property_address": property_address,
            "last_call_date": last_call_date,
            "last_call_status": last_call_status,
            "ai_summary": ai_summary,
            "skip_reason": skip_reason,
            "can_call": can_call,
        })

    leads_out.sort(key=lambda x: (not x["can_call"], x["name"]))
    return JSONResponse({"leads": leads_out, "total": len(leads_out)})


@app.post("/api/trigger-call")
async def api_trigger_call(request: Request):
    """Trigger a VAPI outbound call for a single lead."""
    import json as _json

    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Invalid JSON: {e}"}, status_code=400)

    contact_id = body.get("contact_id", "")
    name = body.get("name", "")
    phone = body.get("phone", "")
    property_address = body.get("property_address", "")

    if not phone:
        return JSONResponse({"ok": False, "error": "phone is required"}, status_code=400)

    # Normalize to E.164
    normalized = phone.strip().replace(" ", "").replace("-", "")
    if not normalized.startswith("+"):
        normalized = "+1" + normalized.lstrip("1")

    # If property_address is missing, fetch it from GHL contact custom fields
    if not property_address and contact_id:
        try:
            from lease_agent import parse_custom_fields as _parse_cf
            async with httpx.AsyncClient(timeout=15) as _gc:
                _cr = await _gc.get(
                    f"{GHL_API_BASE}/contacts/{contact_id}",
                    headers=ghl_headers(),
                )
                if _cr.status_code == 200:
                    _contact_data = _cr.json().get("contact", _cr.json())
                    _custom = _parse_cf(_contact_data.get("customFields", []))
                    property_address = _custom.get("property_address", "")
                    logger.info(f"trigger-call: fetched property_address='{property_address}' for {contact_id}")
        except Exception as _ge:
            logger.warning(f"trigger-call: could not fetch contact details: {_ge}")

    vapi_payload = {
        "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        "assistantId": VAPI_ASSISTANT_ID,
        "customer": {"number": normalized},
        "assistantOverrides": {
            "variableValues": {
                "name": name,
                "first_name": name.split()[0] if name else "",
                "property_address": property_address,
                "contact_id": contact_id,
            }
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{VAPI_BASE}/call",
                headers={"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"},
                json=vapi_payload,
            )
        if resp.status_code not in (200, 201):
            return JSONResponse({"ok": False, "error": f"VAPI error {resp.status_code}: {resp.text[:200]}"})

        call_data = resp.json()
        call_id = call_data.get("id", "")

        logger.info(f"trigger-call: called {name} ({normalized}), property='{property_address}', call_id={call_id}")
        return JSONResponse({"ok": True, "call_id": call_id, "property_address": property_address})

    except Exception as e:
        logger.error(f"trigger-call error: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)[:300]})


# ── Dashboard HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TPMD — Lease Agent</title>
<style>
  :root {
    --purple: #7B2FBE;
    --blue: #4C6EF5;
    --grad: linear-gradient(135deg, #7B2FBE, #4C6EF5);
    --bg: #F5F4F8;
    --card: #FFFFFF;
    --card2: #F8F7FC;
    --border: #E2DDF0;
    --text: #1A1035;
    --muted: #7B6FA0;
    --faint: #B0A8CC;
    --green: #059669;
    --amber: #D97706;
    --red: #DC2626;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); }

  /* Header */
  .header {
    background: var(--grad);
    padding: 10px 24px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 2px 16px rgba(123,47,190,0.35);
  }
  .header-logo { display: flex; align-items: center; gap: 12px; }
  .header-logo img { height: 44px; object-fit: contain; }
  .header-logo h1 { font-size: 15px; font-weight: 600; color: rgba(255,255,255,0.9); }
  .header-right { display: flex; gap: 10px; align-items: center; }
  .scan-time { color: rgba(255,255,255,0.65); font-size: 12px; }

  /* Scheduler bar */
  .sched-bar {
    background: white;
    padding: 7px 24px;
    display: flex; align-items: center; gap: 16px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    box-shadow: 0 1px 0 var(--border);
  }
  .sched-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .sched-dot.live { background: var(--green); animation: pulse 2s infinite; }
  .sched-dot.dry { background: var(--amber); }
  .sched-dot.off { background: var(--faint); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .sched-info { color: var(--muted); }
  .sched-info strong { color: var(--text); }
  .sched-toggle { padding: 3px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--muted); transition: all 0.15s; }
  .sched-toggle:hover { border-color: var(--purple); color: var(--purple); }
  .sched-toggle.live { background: var(--green); color: white; border-color: var(--green); }

  /* Buttons */
  .btn { padding: 8px 18px; border: none; border-radius: 7px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-primary { background: white; color: var(--purple); box-shadow: 0 1px 4px rgba(0,0,0,0.15); }
  .btn-primary:hover:not(:disabled) { background: #f3f0ff; transform: translateY(-1px); box-shadow: 0 3px 10px rgba(0,0,0,0.15); }
  .btn-success { background: var(--green); color: white; } .btn-success:hover:not(:disabled) { background: #047857; }
  .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--muted); }
  .btn-outline:hover { border-color: var(--purple); color: var(--purple); }
  .btn-sm { padding: 4px 10px; font-size: 11px; }
  .btn-voice { background: var(--grad); color: white; }
  .btn-voice:hover:not(:disabled) { box-shadow: 0 2px 10px rgba(123,47,190,0.4); }

  /* Stats */
  .stats { display: flex; gap: 10px; padding: 14px 24px; flex-wrap: wrap; }
  .stat {
    background: white;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 18px;
    min-width: 115px;
    position: relative;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(123,47,190,0.06);
  }
  .stat::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--grad); }
  .stat .n { font-size: 26px; font-weight: 700; color: var(--text); }
  .stat .l { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-top: 2px; }
  .stat.act .n { color: var(--amber); }
  .stat.sms .n { color: var(--blue); }
  .stat.stg .n { color: var(--purple); }
  .stat.skp .n { color: var(--faint); }
  .stat.apr .n { color: var(--green); }

  /* Filters */
  .filters { padding: 8px 24px; display: flex; gap: 6px; flex-wrap: wrap; }
  .fbtn { padding: 5px 14px; border-radius: 20px; font-size: 11px; font-weight: 500; cursor: pointer; border: 1px solid var(--border); background: white; color: var(--muted); transition: all 0.15s; }
  .fbtn:hover { border-color: var(--purple); color: var(--purple); }
  .fbtn.on { background: var(--grad); color: white; border-color: transparent; }

  /* Table */
  .tc { padding: 0 24px 100px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; padding: 10px 12px;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px;
    color: var(--muted);
    border-bottom: 2px solid var(--border);
    position: sticky; top: 0; background: var(--bg); z-index: 10;
  }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; background: white; }
  tr:hover td { background: var(--card2); }
  tr.done td { opacity: 0.4; }

  /* Badges */
  .badge { display: inline-block; padding: 3px 9px; border-radius: 10px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .s-new  { background: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE; }
  .s-ver  { background: #EFF6FF; color: #0369A1; border: 1px solid #BAE6FD; }
  .s-call { background: #FAF5FF; color: #7E22CE; border: 1px solid #E9D5FF; }
  .s-id   { background: #ECFEFF; color: #0E7490; border: 1px solid #A5F3FC; }
  .s-show { background: #F0FDF4; color: #15803D; border: 1px solid #BBF7D0; }
  .s-feed { background: #FFF7ED; color: #C2410C; border: 1px solid #FED7AA; }
  .s-app  { background: #F0FDFA; color: #0F766E; border: 1px solid #99F6E4; }
  .s-won  { background: #F0FDF4; color: #166534; border: 1px solid #BBF7D0; }
  .s-lost { background: #FEF2F2; color: #991B1B; border: 1px solid #FECACA; }
  .a-sms  { background: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE; }
  .a-stg  { background: #FAF5FF; color: #7E22CE; border: 1px solid #E9D5FF; }
  .a-both { background: linear-gradient(135deg, #FAF5FF, #EFF6FF); color: #6D28D9; border: 1px solid #C4B5FD; }
  .a-call { background: #FEF08A; color: #854D0E; border: 1px solid #FCD34D; }
  .a-skip { background: #F9FAFB; color: #9CA3AF; border: 1px solid #E5E7EB; }
  .a-err  { background: #FEF2F2; color: #991B1B; border: 1px solid #FECACA; }

  /* Lead cell */
  .lead-name { font-weight: 600; font-size: 13px; color: var(--text); }
  .lead-phone { font-size: 11px; color: var(--muted); }
  .lead-addr { font-size: 11px; color: var(--faint); margin-top: 2px; }
  .lead-tags { margin-top: 3px; }
  .tag { display: inline-block; padding: 1px 7px; border-radius: 8px; font-size: 10px; background: #F3F0FF; color: var(--purple); border: 1px solid #E9D5FF; margin-right: 3px; }

  /* Activity cell */
  .act-row { font-size: 11px; color: var(--muted); margin-bottom: 3px; display: flex; gap: 6px; align-items: center; }
  .act-label { color: var(--faint); min-width: 70px; }
  .act-val { color: var(--text); }
  .act-warn { color: var(--amber); font-weight: 600; }
  .act-ok { color: var(--green); font-weight: 600; }
  .act-bad { color: var(--red); }
  .dir-in { color: var(--green); } .dir-out { color: var(--blue); }
  .msg-preview { font-size: 11px; color: var(--muted); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }

  /* Decision cell */
  .decision-msg {
    font-size: 12px; color: var(--text); line-height: 1.45;
    max-width: 280px;
    background: #F8F7FC;
    border: 1px solid var(--border);
    border-left: 3px solid var(--purple);
    padding: 6px 8px; border-radius: 0 6px 6px 0; margin: 4px 0; position: relative;
  }
  .decision-msg .edit-btn { position: absolute; top: 4px; right: 4px; font-size: 10px; background: white; border: 1px solid var(--border); color: var(--muted); border-radius: 4px; padding: 2px 6px; cursor: pointer; }
  .decision-msg .edit-btn:hover { border-color: var(--purple); color: var(--purple); }
  .decision-reason { font-size: 11px; color: var(--muted); max-width: 280px; line-height: 1.3; }
  .stage-change { font-size: 11px; color: var(--purple); font-weight: 600; margin: 2px 0; }
  .tmpl-select { margin-top: 4px; }
  .tmpl-select select { background: white; color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 3px 6px; font-size: 11px; max-width: 200px; }

  /* Approve checkbox */
  .chk { width: 28px; height: 28px; border-radius: 6px; border: 2px solid var(--border); background: white; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s; font-size: 14px; color: white; }
  .chk:hover { border-color: var(--purple); }
  .chk.on { background: var(--grad); border-color: transparent; }

  /* Bottom bar */
  .bar {
    padding: 12px 24px;
    background: white;
    border-top: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 20;
    box-shadow: 0 -2px 12px rgba(0,0,0,0.08);
  }
  .bar .info { font-size: 13px; color: var(--muted); }
  .bar .info strong { color: var(--green); }

  /* Loading / empty */
  .center { display: flex; align-items: center; justify-content: center; padding: 60px; flex-direction: column; gap: 12px; }
  .spin { width: 36px; height: 36px; border: 3px solid var(--border); border-top-color: var(--purple); border-radius: 50%; animation: sp 0.7s linear infinite; }
  @keyframes sp { to { transform: rotate(360deg); } }

  /* Modal */
  .modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 100; align-items: center; justify-content: center; }
  .modal-bg.show { display: flex; }
  .modal { background: white; border: 1px solid var(--border); border-radius: 14px; padding: 24px; max-width: 500px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.15); }
  .modal h3 { margin-bottom: 12px; font-size: 16px; color: var(--purple); }
  .modal textarea { width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-size: 13px; min-height: 80px; resize: vertical; font-family: inherit; }
  .modal .actions { display: flex; gap: 8px; margin-top: 12px; justify-content: flex-end; }

  /* Stage-grouped view */
  .stage-section { margin: 0 24px 6px; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(123,47,190,0.05); }
  .stage-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 16px; background: white; cursor: pointer;
    border-bottom: 1px solid var(--border); user-select: none;
    transition: background 0.1s;
  }
  .stage-header:hover { background: var(--card2); }
  .stage-header.collapsed { border-bottom: none; }
  .stage-toggle { font-size: 11px; color: var(--faint); transition: transform 0.2s; width: 14px; }
  .stage-toggle.collapsed { transform: rotate(-90deg); }
  .stage-count { background: var(--purple); color: white; border-radius: 10px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
  .stage-count.zero { background: var(--faint); }
  .stage-act-count { background: var(--amber); color: white; border-radius: 10px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
  .stage-avg { font-size: 11px; color: var(--faint); margin-left: auto; }
  .stage-body { background: white; }
  .stage-body.collapsed { display: none; }
  .stage-row {
    display: grid; grid-template-columns: 28px 1fr 180px 90px 110px 32px;
    gap: 8px; align-items: center; padding: 9px 16px;
    border-bottom: 1px solid var(--border); font-size: 12px;
    transition: background 0.1s;
  }
  .stage-row:last-child { border-bottom: none; }
  .stage-row:hover { background: var(--card2); }
  .sr-lead { min-width: 0; }
  .sr-name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sr-sub { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sr-msg { font-size: 11px; color: var(--muted); min-width: 0; }
  .sr-msg-body { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
  .sr-msg-meta { color: var(--faint); font-size: 10px; }
  .sr-action { display: flex; flex-direction: column; gap: 3px; }
  .sr-approve { width: 28px; height: 28px; border-radius: 6px; border: 2px solid var(--border); background: white; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 13px; color: white; transition: all 0.15s; flex-shrink: 0; }
  .sr-approve:hover { border-color: var(--purple); }
  .sr-approve.on { background: var(--grad); border-color: transparent; }
  .sr-decision { font-size: 11px; background: #F8F7FC; border: 1px solid var(--border); border-left: 3px solid var(--purple); padding: 5px 7px; border-radius: 0 5px 5px 0; position: relative; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .sr-decision:hover { background: #F0EDFF; }

  /* Test chat panel */
  #testChatPanel {
    position: fixed; right: 0; top: 0; bottom: 0; width: 380px;
    background: white; border-left: 1px solid var(--border);
    box-shadow: -4px 0 24px rgba(0,0,0,0.12);
    z-index: 50; display: flex; flex-direction: column;
    transform: translateX(100%); transition: transform 0.3s ease;
  }
  #testChatPanel.open { transform: translateX(0); }
  .tc-header { background: var(--grad); padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
  .tc-header h3 { color: white; font-size: 14px; font-weight: 700; }
  .tc-close { background: rgba(255,255,255,0.2); border: none; color: white; width: 26px; height: 26px; border-radius: 50%; cursor: pointer; font-size: 14px; line-height: 1; }
  .tc-config { padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg); flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; }
  .tc-config label { font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .tc-config select, .tc-config input { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; font-size: 12px; background: white; color: var(--text); outline: none; }
  .tc-config select:focus, .tc-config input:focus { border-color: var(--purple); }
  .tc-messages { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .tc-msg-lead { align-self: flex-end; background: #EFF6FF; border-radius: 12px 12px 2px 12px; padding: 8px 12px; max-width: 85%; font-size: 12px; color: #1E3A5F; }
  .tc-msg-bot { align-self: flex-start; background: #F3F0FF; border-radius: 12px 12px 12px 2px; padding: 8px 12px; max-width: 85%; font-size: 12px; color: var(--text); }
  .tc-msg-bot .tc-reasoning { font-size: 10px; color: var(--faint); margin-top: 4px; font-style: italic; }
  .tc-msg-bot .tc-action-badge { display: inline-block; padding: 2px 6px; border-radius: 8px; font-size: 10px; font-weight: 700; margin-bottom: 4px; background: #E9D5FF; color: #6D28D9; }
  .tc-msg-bot .tc-stage-change { font-size: 10px; color: var(--purple); font-weight: 600; margin-top: 2px; }
  .tc-msg-skip { align-self: center; color: var(--faint); font-size: 11px; font-style: italic; }
  .tc-input-row { padding: 10px 12px; border-top: 1px solid var(--border); display: flex; gap: 8px; flex-shrink: 0; }
  .tc-input { flex: 1; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; outline: none; resize: none; height: 38px; font-family: inherit; }
  .tc-input:focus { border-color: var(--purple); }
  .tc-send { background: var(--grad); color: white; border: none; border-radius: 8px; padding: 8px 14px; cursor: pointer; font-size: 13px; font-weight: 700; flex-shrink: 0; }
  .tc-send:disabled { opacity: 0.5; cursor: not-allowed; }

  /* Call Center panel toast */
  .cc-toast {
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
    background: #1e293b; color: white; padding: 10px 20px; border-radius: 8px;
    font-size: 13px; font-weight: 600; z-index: 9999;
    opacity: 0; transition: opacity 0.3s ease; pointer-events: none;
  }
  .cc-toast.show { opacity: 1; }

  /* Recent interactions */
  .ri-section { margin: 12px 24px; border-radius: 10px; border: 1px solid var(--border); background: white; overflow: hidden; }
  .ri-header { padding: 10px 16px; background: white; cursor: pointer; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--border); }
  .ri-header:hover { background: var(--card2); }
  .ri-header.collapsed { border-bottom: none; }
  .ri-body { max-height: 400px; overflow-y: auto; }
  .ri-row { padding: 10px 16px; border-bottom: 1px solid var(--border); display: grid; grid-template-columns: 130px 120px 1fr; gap: 8px; align-items: start; font-size: 12px; cursor: pointer; }
  .ri-row:last-child { border-bottom: none; }
  .ri-row:hover { background: var(--card2); }
  .ri-name { font-weight: 600; color: var(--text); }
  .ri-time { font-size: 10px; color: var(--faint); }
  .ri-msgs { min-width: 0; }
  .ri-bubble-in { background: #F0FDF4; border-left: 2px solid #10B981; padding: 3px 7px; border-radius: 0 4px 4px 0; font-size: 11px; color: #065F46; margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ri-bubble-out { background: #EFF6FF; border-left: 2px solid #3B82F6; padding: 3px 7px; border-radius: 0 4px 4px 0; font-size: 11px; color: #1E3A5F; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ri-no-data { padding: 20px; text-align: center; color: var(--faint); font-size: 12px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-logo">
    <img src="/logo2.png" height="44" alt="TPMD Logo">
    <h1>The Property Management Doctor <span>— Lease Agent</span></h1>
  </div>
  <div class="header-right">
    <span class="scan-time" id="scanTime"></span>
    <button class="btn btn-outline" style="color:#4ade80;border-color:rgba(74,222,128,0.5)" onclick="toggleCallCenter()">📞 Dial</button>
    <button class="btn btn-outline" style="color:#C4B5FD;border-color:rgba(196,181,253,0.5)" onclick="toggleCallLog()">📞 Call Log</button>
    <button class="btn btn-outline" style="color:#A5F3FC;border-color:rgba(165,243,252,0.5)" onclick="toggleTestChat()">🧪 Test Bot</button>
    <button class="btn btn-outline" style="color:white;border-color:rgba(255,255,255,0.4)" onclick="openTrainModal()">🎓 Train Bot</button>
    <button class="btn btn-outline" style="color:#86EFAC;border-color:rgba(134,239,172,0.5)" onclick="openPropertiesModal()">🏠 Properties</button>
    <button class="btn btn-outline" style="color:#FCD34D;border-color:rgba(252,211,77,0.5)" onclick="openStaleModal()">🧹 Clean Up Stale</button>
    <button class="btn btn-primary" id="scanBtn" onclick="startScan()">Scan All Leads</button>
  </div>
</div>

<!-- Train Bot Modal -->
<div id="trainModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;overflow:auto">
  <div style="background:white;max-width:680px;margin:40px auto;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,0.3)">
    <!-- Modal header -->
    <div style="background:linear-gradient(135deg,#7B2FBE,#4C6EF5);padding:16px 20px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="color:white;font-weight:700;font-size:16px">🎓 Train Bot</span>
        <span style="color:rgba(255,255,255,0.7);font-size:12px;margin-left:10px">Changes take effect immediately</span>
      </div>
      <button onclick="closeTrainModal()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
    <!-- Tabs -->
    <div style="display:flex;border-bottom:1px solid #E2DDF0">
      <button id="tabRules" onclick="switchTab('rules')" style="flex:1;padding:12px;border:none;background:white;font-weight:600;font-size:13px;cursor:pointer;border-bottom:3px solid #7B2FBE;color:#7B2FBE">📋 Custom Rules</button>
      <button id="tabFeedback" onclick="switchTab('feedback')" style="flex:1;padding:12px;border:none;background:white;font-weight:500;font-size:13px;cursor:pointer;border-bottom:3px solid transparent;color:#7B6FA0">💬 Message Feedback</button>
    </div>
    <!-- Rules tab -->
    <div id="panelRules" style="padding:20px">
      <p style="font-size:12px;color:#7B6FA0;margin-bottom:10px">Write one rule per line. These become permanent instructions for the bot.</p>
      <p style="font-size:11px;color:#B0A8CC;margin-bottom:10px">Examples: <code style="background:#F5F4F8;padding:1px 5px;border-radius:3px">Always mention that parking is free.</code> &nbsp; <code style="background:#F5F4F8;padding:1px 5px;border-radius:3px">Never mention competing properties.</code></p>
      <textarea id="botRulesText" style="width:100%;height:200px;border:1px solid #E2DDF0;border-radius:8px;padding:12px;font-size:13px;font-family:monospace;resize:vertical;color:#1A1035;outline:none;line-height:1.6" placeholder="# Add custom rules here, one per line..."></textarea>
      <div style="display:flex;justify-content:flex-end;margin-top:12px">
        <button id="saveRulesBtn" onclick="saveBotRules()" style="background:linear-gradient(135deg,#7B2FBE,#4C6EF5);color:white;border:none;padding:8px 22px;border-radius:7px;font-weight:600;cursor:pointer;font-size:13px">Save Rules</button>
      </div>
    </div>
    <!-- Feedback tab -->
    <div id="panelFeedback" style="display:none;padding:20px">
      <p style="font-size:12px;color:#7B6FA0;margin-bottom:14px">Rate recent bot responses. When you click ❌ you can explain what was wrong — it becomes a rule automatically.</p>
      <div id="feedbackList" style="display:flex;flex-direction:column;gap:10px;max-height:420px;overflow-y:auto">
        <div style="color:#B0A8CC;font-size:13px;text-align:center;padding:30px">Loading recent messages…</div>
      </div>
    </div>
  </div>
</div>

<!-- Stale Leads Modal -->
<div id="staleModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;overflow:auto">
  <div style="background:white;max-width:640px;margin:40px auto;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,0.3)">
    <div style="background:linear-gradient(135deg,#92400E,#D97706);padding:16px 20px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="color:white;font-weight:700;font-size:16px">🧹 Clean Up Stale Leads</span>
        <span style="color:rgba(255,255,255,0.7);font-size:12px;margin-left:10px">No inbound reply in 10+ days</span>
      </div>
      <button onclick="closeStaleModal()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
    <div style="padding:20px">
      <p style="font-size:12px;color:#7B6FA0;margin-bottom:14px">Excluded: Application Sent, Showing Scheduled (future), Leased/Won, Lost.</p>
      <div id="staleLoading" style="text-align:center;padding:30px;color:#94a3b8;font-size:13px">Click "Scan" to find stale leads…</div>
      <div id="staleList" style="display:none;max-height:360px;overflow-y:auto;display:none;flex-direction:column;gap:8px"></div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px">
        <button onclick="scanStaleLeads()" id="staleScanBtn" style="background:#FEF3C7;color:#92400E;border:1px solid #FCD34D;padding:8px 16px;border-radius:7px;font-weight:600;cursor:pointer;font-size:13px">🔍 Scan</button>
        <div style="display:flex;gap:8px;align-items:center">
          <span id="staleStatus" style="font-size:12px;color:#6B7280"></span>
          <button id="staleArchiveBtn" onclick="archiveStaleLeads()" style="display:none;background:linear-gradient(135deg,#92400E,#D97706);color:white;border:none;padding:8px 20px;border-radius:7px;font-weight:600;cursor:pointer;font-size:13px">Move All to Lost</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Properties Manager Modal -->
<div id="propertiesModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;overflow:auto">
  <div style="background:white;max-width:600px;margin:40px auto;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,0.3)">
    <div style="background:linear-gradient(135deg,#059669,#0D9488);padding:16px 20px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="color:white;font-weight:700;font-size:16px">🏠 Properties</span>
        <span style="color:rgba(255,255,255,0.7);font-size:12px;margin-left:10px">Manage property availability — used in Test Bot</span>
      </div>
      <button onclick="closePropertiesModal()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
    <div style="padding:20px">
      <div id="propList" style="display:flex;flex-direction:column;gap:8px;max-height:380px;overflow-y:auto;margin-bottom:12px"></div>
      <button onclick="addProperty()" style="background:#F0FDF4;color:#059669;border:1px solid #BBF7D0;padding:8px 16px;border-radius:7px;font-weight:600;cursor:pointer;font-size:13px;width:100%">+ Add Property</button>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:12px">
        <span id="propSaveStatus" style="font-size:12px;color:#10B981"></span>
        <button onclick="saveProperties()" style="background:linear-gradient(135deg,#059669,#0D9488);color:white;border:none;padding:8px 22px;border-radius:7px;font-weight:600;cursor:pointer;font-size:13px">Save All</button>
      </div>
    </div>
  </div>
</div>

<!-- Test Chat Panel -->
<div id="testChatPanel">
  <div class="tc-header">
    <h3>🧪 Test Bot Chat</h3>
    <button class="tc-close" onclick="toggleTestChat()">✕</button>
  </div>
  <div class="tc-config">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div>
        <label>Name</label>
        <input id="tcName" value="Test Lead" placeholder="Lead name">
      </div>
      <div>
        <label>Stage</label>
        <select id="tcStage">
          <option>New Lead</option>
          <option>Verification Auto-Sent</option>
          <option>Call: No Answer</option>
          <option>Call: Answered</option>
          <option>ID Verified</option>
          <option>Showing Scheduled</option>
          <option>Tenant Feedback</option>
        </select>
      </div>
    </div>
    <div>
      <label>Property</label>
      <select id="tcProperty" onchange="onTcPropertyChange()" style="width:100%;border:1px solid var(--border);border-radius:6px;padding:5px 8px;font-size:12px;background:white;color:var(--text);outline:none">
        <option value="">— Select property —</option>
      </select>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div>
        <label>Lock Code (live)</label>
        <input id="tcLockCode" placeholder="e.g. 1234">
      </div>
      <div>
        <label style="display:flex;align-items:center;gap:4px">🔑 Backup Code <span style="font-size:10px;color:var(--muted);font-weight:400">(auto-filled)</span></label>
        <input id="tcBackupLockCode" placeholder="From property settings" style="background:#fffbe6">
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end">
      <button onclick="clearTestChat()" style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:5px 10px;border-radius:6px;font-size:11px;cursor:pointer">Clear chat</button>
    </div>
  </div>
  <div class="tc-messages" id="tcMessages">
    <div style="text-align:center;color:var(--faint);font-size:12px;padding:20px">Type a message below as if you were a lead.<br>The bot will respond (no real SMS sent).</div>
  </div>
  <div class="tc-input-row">
    <textarea class="tc-input" id="tcInput" placeholder="Type as lead…" onkeydown="tcKeyDown(event)"></textarea>
    <button class="tc-send" id="tcSend" onclick="sendTestMessage()">→</button>
  </div>
</div>

<div class="sched-bar">
  <span class="sched-dot dry" id="schedDot"></span>
  <span class="sched-info">Auto-scan: <strong id="schedMode">DRY RUN</strong> — every 2h (9am, 11am, 1pm, 3pm, 5pm, 7pm ET)</span>
  <span class="sched-info" id="schedLast"></span>
  <button class="sched-toggle" id="schedToggle" onclick="toggleScheduler()">Switch to LIVE</button>
  <span class="sched-info" style="margin-left:auto">Webhook: <code style="color:#38bdf8">POST /api/webhook/inbound</code> | <a href="/api/webhook-log" target="_blank" style="color:#38bdf8; text-decoration:underline">Activity Log</a></span>
</div>

<div class="stats" id="stats" style="display:none">
  <div class="stat"><div class="n" id="nTotal">0</div><div class="l">Total</div></div>
  <div class="stat act"><div class="n" id="nAct">0</div><div class="l">Actions</div></div>
  <div class="stat sms"><div class="n" id="nSms">0</div><div class="l">SMS</div></div>
  <div class="stat stg"><div class="n" id="nStg">0</div><div class="l">Stage Moves</div></div>
  <div class="stat" style="border-top:3px solid #F59E0B"><div class="n" id="nCall" style="color:#D97706">0</div><div class="l">Call Bot</div></div>
  <div class="stat skp"><div class="n" id="nSkp">0</div><div class="l">Skipped</div></div>
  <div class="stat apr"><div class="n" id="nApr">0</div><div class="l">Approved</div></div>
</div>

<div class="filters" id="filters" style="display:none">
  <button class="fbtn on" onclick="setF('all',this)">All</button>
  <button class="fbtn" onclick="setF('action',this)">Actions Only</button>
  <button class="fbtn" onclick="setF('send_sms',this)">SMS</button>
  <button class="fbtn" onclick="setF('update_stage',this)">Stage Updates</button>
  <button class="fbtn" onclick="setF('call_for_showing',this)">Call Bot</button>
  <button class="fbtn" onclick="setF('skip',this)">Skipped</button>
  <button class="fbtn" onclick="setF('urgent',this)">Urgent (3+ days)</button>
</div>

<div id="content">
  <div class="center">
    <h2 style="color:#94a3b8;font-size:18px">Ready to scan</h2>
    <p style="color:#64748b;font-size:13px">Click "Scan All Leads" to analyze your pipeline</p>
  </div>
</div>

<!-- Recent Interactions Panel -->
<div class="ri-section" id="riSection" style="display:none">
  <div class="ri-header" onclick="toggleRI()" id="riHeader">
    <span class="stage-toggle" id="riToggle">▼</span>
    <span style="font-weight:600;font-size:13px;color:var(--text)">Recent Bot Activity</span>
    <span class="stage-count" id="riCount" style="background:var(--blue)">0</span>
    <span style="font-size:11px;color:var(--faint);margin-left:auto">Live feed — updates on inbound messages</span>
    <button onclick="event.stopPropagation();loadRI()" style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:5px;font-size:11px;cursor:pointer;margin-left:8px">↻ Refresh</button>
  </div>
  <div class="ri-body" id="riBody">
    <div class="ri-no-data">No interactions yet. The bot will appear here when it handles inbound messages.</div>
  </div>
</div>

<!-- Call Center floating panel -->
<div id="callCenterPanel" style="display:none;position:fixed;top:0;right:0;bottom:0;width:780px;background:white;border-left:1px solid var(--border);box-shadow:-4px 0 24px rgba(0,0,0,0.1);z-index:300;display:flex;flex-direction:column;transform:translateX(100%);transition:transform 0.3s ease">
  <div style="background:linear-gradient(135deg,#059669,#0d9488);padding:14px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:white;font-weight:700;font-size:15px">📞 Call Center</span>
      <span id="callCenterCount" style="background:rgba(255,255,255,0.25);color:white;padding:1px 8px;border-radius:12px;font-size:12px;font-weight:600">0</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button onclick="triggerCalls()" id="ccCallSelectedBtn" style="background:rgba(255,255,255,0.2);border:1px solid rgba(255,255,255,0.4);color:white;padding:4px 12px;border-radius:6px;font-size:12px;cursor:pointer;font-weight:600">📞 Call Selected</button>
      <button onclick="loadCallCenter()" style="background:rgba(255,255,255,0.2);border:none;color:white;padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer">↻ Refresh</button>
      <button onclick="toggleCallCenter()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
  </div>
  <div style="flex:1;overflow-y:auto">
    <div id="callCenterLoading" style="text-align:center;padding:30px;color:#94a3b8;font-size:13px">Loading…</div>
    <table id="callCenterTable" style="display:none;width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="background:#f0fdf4;border-bottom:2px solid #bbf7d0;position:sticky;top:0">
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46;width:32px">
            <input type="checkbox" id="ccSelectAll" onchange="ccToggleAll(this.checked)" style="cursor:pointer">
          </th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">Name</th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">Stage</th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">Last Call</th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">Status</th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">AI Summary</th>
          <th style="padding:10px 10px;text-align:left;font-weight:600;color:#065f46">Action</th>
        </tr>
      </thead>
      <tbody id="callCenterRows"></tbody>
    </table>
    <div id="callCenterEmpty" style="display:none;text-align:center;padding:40px;color:#94a3b8;font-size:13px">No eligible leads found.</div>
  </div>
</div>
<div class="cc-toast" id="ccToast"></div>

<!-- Call Log floating panel -->
<div id="callLogPanel" style="display:none;position:fixed;top:0;right:0;bottom:0;width:680px;background:white;border-left:1px solid var(--border);box-shadow:-4px 0 24px rgba(0,0,0,0.1);z-index:300;display:flex;flex-direction:column;transform:translateX(100%);transition:transform 0.3s ease">
  <div style="background:linear-gradient(135deg,#7B2FBE,#4C6EF5);padding:14px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:white;font-weight:700;font-size:15px">📞 Call Log</span>
      <span id="callLogCount" style="background:rgba(255,255,255,0.25);color:white;padding:1px 8px;border-radius:12px;font-size:12px;font-weight:600">0</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button onclick="loadCallLog()" style="background:rgba(255,255,255,0.2);border:none;color:white;padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer">↻ Refresh</button>
      <button onclick="toggleCallLog()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:16px;line-height:1">✕</button>
    </div>
  </div>
  <div style="flex:1;overflow-y:auto">
    <div id="callLogLoading" style="text-align:center;padding:30px;color:#94a3b8;font-size:13px">Loading…</div>
    <table id="callLogTable" style="display:none;width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="background:#f8f7fc;border-bottom:2px solid var(--border);position:sticky;top:0">
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Name</th>
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Date (ET)</th>
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Connected</th>
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Duration</th>
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Outcome</th>
          <th style="padding:10px 14px;text-align:left;font-weight:600;color:var(--text)">Summary</th>
        </tr>
      </thead>
      <tbody id="callLogRows"></tbody>
    </table>
    <div id="callLogEmpty" style="display:none;text-align:center;padding:40px;color:#94a3b8;font-size:13px">אין שיחות עדיין. שיחות Voice Bot יופיעו כאן.</div>
  </div>
</div>

<div class="bar" id="bar" style="display:none">
  <div class="info"><strong id="nAprBar">0</strong> actions approved</div>
  <div style="display:flex;gap:8px">
    <button class="btn btn-outline btn-sm" onclick="approveAll()">Approve All</button>
    <button class="btn btn-success" id="exBtn" onclick="execApproved()">Execute Approved</button>
  </div>
</div>

<!-- Edit message modal -->
<div class="modal-bg" id="editModal">
  <div class="modal">
    <h3>Edit Message</h3>
    <p style="font-size:12px;color:#64748b;margin-bottom:8px" id="editFor"></p>
    <textarea id="editText"></textarea>
    <div style="font-size:11px;color:#64748b;margin-top:4px"><span id="charCount">0</span> chars</div>
    <div class="actions">
      <button class="btn btn-outline btn-sm" onclick="closeEdit()">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>

<script>
let leads = [], filter = 'all', editId = null;
const STAGE_ORDER = ['New Lead','Verification Auto-Sent','Call: No Answer','Call: Answered',
  'Manual Review Needed','Pending ID Review','ID Rejected','ID Verified',
  'Showing Scheduled','Tenant Feedback','Application Sent'];
const stageCollapsed = {};  // track collapsed state per stage

async function startScan() {
  const b = document.getElementById('scanBtn');
  b.disabled = true; b.textContent = 'Scanning...';
  document.getElementById('content').innerHTML = '<div class="center"><div class="spin"></div><div style="color:#94a3b8;font-size:13px">Scanning leads & getting AI decisions...</div><div style="color:#64748b;font-size:11px">~1-2 min for all active leads</div></div>';
  hide('stats'); hide('filters'); hide('bar');
  try { await fetch('/api/scan', {method:'POST'}); await load(); } catch(e) { document.getElementById('content').innerHTML = `<div class="center"><h2 style="color:#f87171">Error: ${e.message}</h2></div>`; }
  b.disabled = false; b.textContent = 'Scan All Leads';
}

async function load() {
  const r = await fetch('/api/leads');
  const d = await r.json();
  leads = d.leads || [];
  if (d.scan_time) {
    const utc = new Date(d.scan_time);
    const israel = utc.toLocaleString('he-IL', {timeZone: 'Asia/Jerusalem', hour: '2-digit', minute: '2-digit'});
    const florida = utc.toLocaleString('en-US', {timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit'});
    document.getElementById('scanTime').textContent = `Last: 🇮🇱 ${israel} | 🇺🇸 ${florida}`;
  }
  if (leads.length) { show('stats'); show('filters'); show('bar'); }
  updateStats(); render();
}

function updateStats() {
  const t = leads.length;
  const act = leads.filter(l => !['skip','error'].includes(l.action)).length;
  const sms = leads.filter(l => l.action?.includes('sms')).length;
  const stg = leads.filter(l => l.action?.includes('stage')).length;
  const call = leads.filter(l => l.action === 'call_for_showing').length;
  const skp = leads.filter(l => l.action === 'skip').length;
  const apr = leads.filter(l => l.approved).length;
  document.getElementById('nTotal').textContent = t;
  document.getElementById('nAct').textContent = act;
  document.getElementById('nSms').textContent = sms;
  document.getElementById('nStg').textContent = stg;
  document.getElementById('nCall').textContent = call;
  document.getElementById('nSkp').textContent = skp;
  document.getElementById('nApr').textContent = apr;
  document.getElementById('nAprBar').textContent = apr;
}

function sc(s) {
  if (s.includes('New')) return 's-new'; if (s.includes('Verification')) return 's-ver';
  if (s.includes('Call')) return 's-call'; if (s.includes('ID')) return 's-id';
  if (s.includes('Showing')) return 's-show'; if (s.includes('Feedback')) return 's-feed';
  if (s.includes('Application')) return 's-app'; if (s.includes('Won')) return 's-won';
  if (s.includes('Lost')) return 's-lost'; return '';
}
function ac(a) {
  if (a === 'send_sms') return 'a-sms'; if (a === 'update_stage') return 'a-stg';
  if (a === 'send_sms_and_update_stage') return 'a-both'; if (a === 'call_for_showing') return 'a-call';
  if (a === 'error') return 'a-err'; return 'a-skip';
}
function al(a) {
  if (a === 'send_sms') return 'SMS'; if (a === 'update_stage') return 'Move';
  if (a === 'send_sms_and_update_stage') return 'SMS+Move'; if (a === 'call_for_showing') return 'Call Bot';
  if (a === 'error') return 'Error'; return 'Skip';
}

function setF(f, el) {
  filter = f;
  document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('on'));
  el.classList.add('on'); render();
}

function toggleStage(stage) {
  stageCollapsed[stage] = !stageCollapsed[stage];
  render();
}

function render() {
  let fl = leads.filter(l => !['Lost','Leased / Won','Application Sent'].includes(l.stage));
  if (filter === 'action') fl = fl.filter(l => !['skip','error'].includes(l.action));
  else if (filter === 'urgent') fl = fl.filter(l => (l.days_since_last_activity || 0) >= 3);
  else if (filter !== 'all') fl = fl.filter(l => l.action === filter);

  if (!fl.length) { document.getElementById('content').innerHTML = '<div class="center"><p style="color:#64748b">No leads match this filter</p></div>'; return; }

  // Group by stage in pipeline order
  const byStage = {};
  for (const s of STAGE_ORDER) byStage[s] = [];
  for (const l of fl) {
    if (!byStage[l.stage]) byStage[l.stage] = [];
    byStage[l.stage].push(l);
  }

  let h = '<div style="padding:0 0 120px">';
  for (const stageName of STAGE_ORDER) {
    const group = byStage[stageName] || [];
    if (!group.length) continue;

    // Sort by last_message_date desc (most recent first)
    group.sort((a, b) => {
      const da = a.last_message_date || '';
      const db = b.last_message_date || '';
      if (db > da) return 1; if (da > db) return -1;
      // secondary: action priority
      const o = {send_sms_and_update_stage:0, send_sms:1, update_stage:2, call_for_showing:3, error:4, skip:5};
      return (o[a.action]??6) - (o[b.action]??6);
    });

    const actionCount = group.filter(l => !['skip','error'].includes(l.action)).length;
    const collapsed = !!stageCollapsed[stageName];
    const avgDays = group.filter(l => l.days_since_last_activity !== null).length
      ? (group.reduce((s, l) => s + (l.days_since_last_activity||0), 0) / group.length).toFixed(1)
      : null;

    h += `<div class="stage-section">
      <div class="stage-header${collapsed?' collapsed':''}" onclick="toggleStage('${stageName}')">
        <span class="stage-toggle${collapsed?' collapsed':''}">▼</span>
        <span class="badge ${sc(stageName)}" style="font-size:12px">${stageName}</span>
        <span class="stage-count${group.length===0?' zero':''}">${group.length}</span>
        ${actionCount ? `<span class="stage-act-count">${actionCount} action${actionCount>1?'s':''}</span>` : ''}
        ${avgDays !== null ? `<span class="stage-avg">avg ${avgDays}d since msg</span>` : ''}
      </div>
      <div class="stage-body${collapsed?' collapsed':''}">`;

    for (const l of group) {
      const isAct = !['skip','error'].includes(l.action);
      const daysCls = (l.days_since_last_activity||0) >= 3 ? 'act-warn' : (l.days_since_last_activity||0) >= 1 ? 'act-val' : 'act-ok';
      const daysText = l.days_since_last_activity !== null ? `${l.days_since_last_activity}d` : '—';
      const dirIcon = l.last_message_direction === 'inbound' ? '↙' : '↗';
      const dirCls = l.last_message_direction === 'inbound' ? 'dir-in' : 'dir-out';
      const decisionPreview = l.message ? `"${esc(l.message.slice(0,80))}${l.message.length>80?'…':''}"` : (l.reasoning ? esc(l.reasoning.slice(0,80)) : '');
      const firstName = l.name?.split(' ')[0] || 'Unknown';

      h += `<div class="stage-row${l.executed?' done':''}">
        <div>
          ${isAct && !l.executed
            ? `<button class="sr-approve${l.approved?' on':''}" onclick="toggleApprove('${l.id}')" title="Approve">${l.approved?'✓':''}</button>`
            : `<div style="width:28px"></div>`}
        </div>
        <div class="sr-lead">
          <div class="sr-name" title="${esc(l.name||'')}">${esc(l.name||'Unknown')}</div>
          <div class="sr-sub">${l.phone||''}</div>
          <div class="sr-sub" style="color:var(--faint)" title="${esc(l.property_address||'')}">${esc(l.property_address||'')}</div>
          ${l.showing_date ? `<div class="sr-sub" style="color:var(--green)">📅 ${l.showing_date}</div>` : ''}
        </div>
        <div class="sr-msg">
          ${l.last_message ? `<div class="sr-msg-body" title="${esc(l.last_message)}">${esc(l.last_message.slice(0,60))}${l.last_message.length>60?'…':''}</div>` : '<div style="color:var(--faint);font-size:11px">No messages</div>'}
          <div class="sr-msg-meta"><span class="${daysCls}">${daysText}</span> <span class="${dirCls}">${l.last_message_direction?dirIcon:''}</span> ${l.last_message_date?l.last_message_date.slice(0,10):''}</div>
        </div>
        <div class="sr-action">
          <span class="badge ${ac(l.action)}" style="font-size:11px">${al(l.action)}</span>
          ${l.new_stage ? `<span style="font-size:10px;color:var(--purple);font-weight:600">→ ${l.new_stage}</span>` : ''}
        </div>
        <div>
          ${isAct && decisionPreview
            ? `<div class="sr-decision" onclick="openEdit('${l.id}')" title="${esc(l.reasoning||'')}">${decisionPreview}</div>`
            : `<div style="font-size:11px;color:var(--faint)">${esc((l.reasoning||'').slice(0,60))}</div>`}
          ${l.available_templates?.length && isAct && !l.executed
            ? `<select style="margin-top:3px;border:1px solid var(--border);border-radius:4px;padding:2px 4px;font-size:10px;color:var(--muted);max-width:110px" onchange="applyTmpl('${l.id}',this.value)"><option value="">Template…</option>${l.available_templates.map(t=>`<option value="${t.id}">${t.name}</option>`).join('')}</select>`
            : ''}
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end">
          ${l.stage === 'ID Verified' || l.stage === 'Call: No Answer'
            ? `<button class="btn btn-voice btn-sm" onclick="triggerVoice('${l.contact_id}','${firstName}')">📞</button>`
            : ''}
          <button style="background:#F5F3FF;color:#7B2FBE;border:1px solid #DDD6FE;border-radius:5px;padding:2px 7px;font-size:10px;cursor:pointer;white-space:nowrap" onclick="openConvReview('${l.id}')">💬 Review</button>
        </div>
      </div>`;
    }
    h += '</div></div>';
  }
  h += '</div>';
  document.getElementById('content').innerHTML = h;
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function show(id) { document.getElementById(id).style.display = 'flex'; }
function hide(id) { document.getElementById(id).style.display = 'none'; }

async function toggleApprove(id) {
  await fetch(`/api/approve/${id}`, {method:'POST'});
  const l = leads.find(x => x.id === id); if (l) l.approved = !l.approved;
  updateStats(); render();
}

async function approveAll() {
  await fetch('/api/approve-all', {method:'POST'});
  leads.forEach(l => { if (!['skip','error'].includes(l.action) && !l.executed) l.approved = true; });
  updateStats(); render();
}

async function execApproved() {
  const n = leads.filter(l => l.approved && !l.executed).length;
  if (!n) { alert('No approved actions'); return; }
  if (!confirm(`Execute ${n} actions? This sends real SMS messages.`)) return;
  const b = document.getElementById('exBtn'); b.disabled = true; b.textContent = 'Executing...';
  const r = await fetch('/api/execute', {method:'POST'});
  const d = await r.json();
  for (const x of d.results) { const l = leads.find(y => y.id === x.id); if (l && x.status === 'success') l.executed = true; }
  b.disabled = false; b.textContent = 'Execute Approved';
  updateStats(); render();
  const ok = d.results.filter(r => r.status === 'success').length;
  alert(`Done! ${ok} executed.`);
}

async function applyTmpl(oppId, tmplId) {
  if (!tmplId) return;
  const r = await fetch(`/api/apply-template/${oppId}/${tmplId}`, {method:'POST'});
  const d = await r.json();
  if (d.message) {
    const l = leads.find(x => x.id === oppId);
    if (l) { l.message = d.message; if (l.action === 'skip') l.action = 'send_sms'; }
    render();
  }
}

function openEdit(id) {
  editId = id;
  const l = leads.find(x => x.id === id);
  document.getElementById('editFor').textContent = `${l.name} — ${l.property_address}`;
  document.getElementById('editText').value = l.message || '';
  document.getElementById('charCount').textContent = (l.message||'').length;
  document.getElementById('editModal').classList.add('show');
  document.getElementById('editText').oninput = function() {
    document.getElementById('charCount').textContent = this.value.length;
  };
}

function closeEdit() { document.getElementById('editModal').classList.remove('show'); editId = null; }

async function saveEdit() {
  const msg = document.getElementById('editText').value;
  await fetch(`/api/update-message/${editId}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({message: msg, action: 'send_sms'})});
  const l = leads.find(x => x.id === editId);
  if (l) { l.message = msg; l.action = 'send_sms'; }
  closeEdit(); render();
}

async function triggerVoice(contactId, name) {
  if (!confirm(`Trigger voice bot call to ${name}?`)) return;
  const r = await fetch(`/api/trigger-voice-bot/${contactId}`, {method:'POST'});
  const d = await r.json();
  if (d.status === 'pending_setup') {
    alert('Voice bot setup needed: Create a GHL workflow with trigger tag "call_for_showing" and connect it to your Voice AI agent.');
  } else {
    alert('Voice bot triggered!');
  }
}

// Scheduler
async function loadScheduler() {
  try {
    const r = await fetch('/api/scheduler');
    const d = await r.json();
    const dot = document.getElementById('schedDot');
    const mode = document.getElementById('schedMode');
    const toggle = document.getElementById('schedToggle');
    const last = document.getElementById('schedLast');

    if (d.dry_run) {
      dot.className = 'sched-dot dry';
      mode.textContent = 'DRY RUN';
      toggle.textContent = 'Switch to LIVE';
      toggle.classList.remove('live');
    } else {
      dot.className = 'sched-dot live';
      mode.textContent = 'LIVE';
      toggle.textContent = 'Switch to DRY RUN';
      toggle.classList.add('live');
    }

    if (d.last_run) {
      const summary = d.last_result_summary;
      last.textContent = 'Last auto: ' + d.last_run.slice(0,16) + ' (' + (summary?.actions || 0) + ' actions)';
    }
  } catch(e) {}
}

async function toggleScheduler() {
  const mode = document.getElementById('schedMode').textContent;
  if (mode === 'DRY RUN') {
    if (!confirm('Switch to LIVE mode? The agent will automatically send SMS messages every hour.')) return;
  }
  await fetch('/api/scheduler/toggle-live', {method:'POST'});
  loadScheduler();
}

load();
loadScheduler();
loadRI();
setInterval(loadScheduler, 30000);
setInterval(loadRI, 60000);  // refresh recent interactions every minute

// ── Train Bot Modal ───────────────────────────────────────────────────────────
function openTrainModal() {
  document.getElementById('trainModal').style.display = 'block';
  document.body.style.overflow = 'hidden';
  loadBotRules();
}
function closeTrainModal() {
  document.getElementById('trainModal').style.display = 'none';
  document.body.style.overflow = '';
}
document.getElementById('trainModal').addEventListener('click', function(e) {
  if (e.target === this) closeTrainModal();
});

function switchTab(tab) {
  document.getElementById('panelRules').style.display = tab === 'rules' ? 'block' : 'none';
  document.getElementById('panelFeedback').style.display = tab === 'feedback' ? 'block' : 'none';
  document.getElementById('tabRules').style.borderBottomColor = tab === 'rules' ? '#7B2FBE' : 'transparent';
  document.getElementById('tabRules').style.color = tab === 'rules' ? '#7B2FBE' : '#7B6FA0';
  document.getElementById('tabFeedback').style.borderBottomColor = tab === 'feedback' ? '#7B2FBE' : 'transparent';
  document.getElementById('tabFeedback').style.color = tab === 'feedback' ? '#7B2FBE' : '#7B6FA0';
  if (tab === 'feedback') loadFeedbackList();
}

async function loadBotRules() {
  const res = await fetch('/api/bot-rules');
  const data = await res.json();
  document.getElementById('botRulesText').value = data.rules || '';
}

async function saveBotRules() {
  const rules = document.getElementById('botRulesText').value;
  const btn = document.getElementById('saveRulesBtn');
  btn.textContent = 'Saving…'; btn.disabled = true;
  const res = await fetch('/api/bot-rules', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rules})
  });
  const data = await res.json();
  btn.textContent = data.status === 'saved' ? '✅ Saved!' : '❌ Error';
  setTimeout(() => { btn.textContent = 'Save Rules'; btn.disabled = false; }, 2000);
}

// ── Stale Leads Modal ────────────────────────────────────────────────────────
let staleLeadsData = [];
function openStaleModal() {
  document.getElementById('staleModal').style.display = 'block';
  document.body.style.overflow = 'hidden';
}
function closeStaleModal() {
  document.getElementById('staleModal').style.display = 'none';
  document.body.style.overflow = '';
}
document.getElementById('staleModal').addEventListener('click', function(e) {
  if (e.target === this) closeStaleModal();
});

async function scanStaleLeads() {
  const btn = document.getElementById('staleScanBtn');
  const loading = document.getElementById('staleLoading');
  const list = document.getElementById('staleList');
  const status = document.getElementById('staleStatus');
  const archiveBtn = document.getElementById('staleArchiveBtn');

  btn.textContent = 'Scanning…'; btn.disabled = true;
  loading.style.display = 'block'; loading.textContent = 'Scanning leads… this may take a minute.';
  list.style.display = 'none';
  archiveBtn.style.display = 'none';
  status.textContent = '';

  const res = await fetch('/api/stale-leads');
  const data = await res.json();
  staleLeadsData = data.stale || [];

  loading.style.display = 'none';
  btn.textContent = '🔍 Scan'; btn.disabled = false;

  if (staleLeadsData.length === 0) {
    status.textContent = '✅ No stale leads found.';
    return;
  }

  list.innerHTML = staleLeadsData.map((l, i) => `
    <div style="display:flex;justify-content:space-between;align-items:center;border:1px solid #FDE68A;border-radius:8px;padding:10px 14px;background:#FFFBEB">
      <div>
        <div style="font-weight:600;font-size:13px;color:#1A1035">${l.name}</div>
        <div style="font-size:11px;color:#92400E">${l.stage} · Last reply: ${l.last_inbound} · Created: ${l.created}</div>
      </div>
      <input type="checkbox" checked data-id="${l.opp_id}" style="width:16px;height:16px;cursor:pointer">
    </div>
  `).join('');
  list.style.display = 'flex';
  status.textContent = `${staleLeadsData.length} stale leads found`;
  archiveBtn.style.display = 'block';
}

async function archiveStaleLeads() {
  const checked = [...document.querySelectorAll('#staleList input[type=checkbox]:checked')];
  const opp_ids = checked.map(el => el.dataset.id);
  if (!opp_ids.length) return;

  const archiveBtn = document.getElementById('staleArchiveBtn');
  const status = document.getElementById('staleStatus');
  archiveBtn.textContent = 'Moving…'; archiveBtn.disabled = true;

  const res = await fetch('/api/stale-leads/archive', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({opp_ids})
  });
  const data = await res.json();
  status.textContent = `✅ ${data.moved} moved to Lost${data.errors ? `, ${data.errors} errors` : ''}`;
  archiveBtn.style.display = 'none';
  await scanStaleLeads();
}

// ── Properties Manager Modal ──────────────────────────────────────────────────
let propertiesList = [];

function openPropertiesModal() {
  document.getElementById('propertiesModal').style.display = 'block';
  document.body.style.overflow = 'hidden';
  loadProperties();
}
function closePropertiesModal() {
  document.getElementById('propertiesModal').style.display = 'none';
  document.body.style.overflow = '';
}
document.getElementById('propertiesModal').addEventListener('click', function(e) {
  if (e.target === this) closePropertiesModal();
});

async function loadProperties() {
  const res = await fetch('/api/properties');
  const data = await res.json();
  propertiesList = data.properties || [];
  renderPropList();
  // Also populate test chat dropdown
  refreshPropertyDropdown();
}

function renderPropList() {
  const el = document.getElementById('propList');
  if (!propertiesList.length) {
    el.innerHTML = '<div style="text-align:center;color:var(--faint);padding:20px;font-size:13px">No properties yet. Click "+ Add Property" below.</div>';
    return;
  }
  el.innerHTML = propertiesList.map((p, i) => `
    <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:${p.available===false?'#FFF5F5':'#F0FDF4'}">
      <div style="display:grid;grid-template-columns:1fr 90px 80px 32px;gap:8px;align-items:center">
        <input value="${esc(p.address||'')}" oninput="propertiesList[${i}].address=this.value" placeholder="Address" style="border:1px solid var(--border);border-radius:5px;padding:5px 8px;font-size:12px;outline:none;width:100%">
        <input value="${esc(p.notes||'')}" oninput="propertiesList[${i}].notes=this.value" placeholder="Notes" style="border:1px solid var(--border);border-radius:5px;padding:5px 8px;font-size:12px;outline:none;width:100%">
        <label style="display:flex;align-items:center;gap:5px;cursor:pointer;font-size:12px;font-weight:600;color:${p.available===false?'var(--red)':'var(--green)'}">
          <input type="checkbox" ${p.available!==false?'checked':''} onchange="propertiesList[${i}].available=this.checked;renderPropList()" style="cursor:pointer">
          ${p.available!==false?'Available':'Unavailable'}
        </label>
        <button onclick="propertiesList.splice(${i},1);renderPropList()" style="background:#FEE2E2;border:none;color:#DC2626;border-radius:5px;cursor:pointer;padding:4px 8px;font-size:13px">✕</button>
      </div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
        <span style="font-size:11px;color:var(--muted);white-space:nowrap">🔑 Backup code:</span>
        <input value="${esc(p.backup_lock_code||'')}" oninput="propertiesList[${i}].backup_lock_code=this.value" placeholder="Static lockbox code (fallback if live code fails)" style="border:1px solid var(--border);border-radius:5px;padding:4px 8px;font-size:12px;outline:none;flex:1;background:#fffbe6">
      </div>
    </div>
  `).join('');
}

function addProperty() {
  const i = propertiesList.length;
  propertiesList.push({address:'', available:true, notes:'', backup_lock_code:''});
  // Append only the new row instead of re-rendering everything (preserves unsaved input values)
  const el = document.getElementById('propList');
  if (el.querySelector('div[style*="text-align:center"]')) el.innerHTML = '';
  const row = document.createElement('div');
  row.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:#F0FDF4';
  row.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 90px 80px 32px;gap:8px;align-items:center">
      <input oninput="propertiesList[${i}].address=this.value" placeholder="Address" style="border:1px solid var(--border);border-radius:5px;padding:5px 8px;font-size:12px;outline:none;width:100%">
      <input oninput="propertiesList[${i}].notes=this.value" placeholder="Notes" style="border:1px solid var(--border);border-radius:5px;padding:5px 8px;font-size:12px;outline:none;width:100%">
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer;font-size:12px;font-weight:600;color:var(--green)">
        <input type="checkbox" checked onchange="propertiesList[${i}].available=this.checked;renderPropList()" style="cursor:pointer">
        Available
      </label>
      <button onclick="propertiesList.splice(${i},1);renderPropList()" style="background:#FEE2E2;border:none;color:#DC2626;border-radius:5px;cursor:pointer;padding:4px 8px;font-size:13px">✕</button>
    </div>
    <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
      <span style="font-size:11px;color:var(--muted);white-space:nowrap">🔑 Backup code:</span>
      <input oninput="propertiesList[${i}].backup_lock_code=this.value" placeholder="Static lockbox code (fallback if live code fails)" style="border:1px solid var(--border);border-radius:5px;padding:4px 8px;font-size:12px;outline:none;flex:1;background:#fffbe6">
    </div>`;
  el.appendChild(row);
  row.querySelector('input').focus();
}

async function saveProperties() {
  const btn = document.querySelector('#propertiesModal button[onclick="saveProperties()"]');
  const status = document.getElementById('propSaveStatus');
  btn.textContent = 'Saving…'; btn.disabled = true;
  const valid = propertiesList.filter(p => p.address?.trim());
  const res = await fetch('/api/properties', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({properties: valid})
  });
  const data = await res.json();
  btn.textContent = 'Save All'; btn.disabled = false;
  status.textContent = data.status === 'saved' ? `✅ Saved ${data.count} properties` : '❌ Error';
  setTimeout(() => status.textContent = '', 3000);
  propertiesList = valid;
  renderPropList();
  refreshPropertyDropdown();
}

function refreshPropertyDropdown() {
  const sel = document.getElementById('tcProperty');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— Select property —</option>' +
    propertiesList.filter(p=>p.address?.trim()).map(p =>
      `<option value="${esc(p.address)}" data-notes="${esc(p.notes||'')}" data-backup-lock-code="${esc(p.backup_lock_code||'')}" ${p.available===false?'style="color:var(--red)"':''}>${esc(p.address)}${p.available===false?' 🚫':''}</option>`
    ).join('');
  if (current) sel.value = current;
  onTcPropertyChange();
}

function onTcPropertyChange() {
  const sel = document.getElementById('tcProperty');
  if (!sel) return;
  const opt = sel.selectedOptions[0];
  const backupInput = document.getElementById('tcBackupLockCode');
  if (backupInput) backupInput.value = opt?.dataset.backupLockCode || '';
}

async function loadFeedbackList() {
  const res = await fetch('/api/webhook-log');
  const data = await res.json();
  const entries = (data.recent || []).filter(e => e.bot_message).reverse();
  const el = document.getElementById('feedbackList');
  if (!entries.length) {
    el.innerHTML = '<div style="color:#B0A8CC;font-size:13px;text-align:center;padding:30px">No bot responses yet. Messages will appear here after the bot replies to leads.</div>';
    return;
  }
  el.innerHTML = entries.map((e, i) => `
    <div id="fb${i}" style="border:1px solid #E2DDF0;border-radius:10px;padding:14px;background:#FAFAF9">
      <div style="font-size:11px;color:#B0A8CC;margin-bottom:6px">${e.timestamp?.slice(0,16).replace('T',' ')} · ${e.lead_name || e.contact_id}</div>
      ${e.message ? `<div style="background:#F0FDF4;border-left:3px solid #10B981;padding:8px 10px;border-radius:4px;font-size:12px;color:#065F46;margin-bottom:6px">🙋 Lead: "${e.message}"</div>` : ''}
      <div style="background:#EFF6FF;border-left:3px solid #3B82F6;padding:8px 10px;border-radius:4px;font-size:12px;color:#1E3A5F;margin-bottom:8px">🤖 Bot: "${e.bot_message}"</div>
      ${e.bot_follow_up ? `<div style="background:#EFF6FF;border-left:3px solid #3B82F6;padding:8px 10px;border-radius:4px;font-size:12px;color:#1E3A5F;margin-bottom:8px">🤖 Follow-up: "${e.bot_follow_up}"</div>` : ''}
      <div style="display:flex;gap:8px;align-items:center">
        <button onclick="rateFeedback(${i},'good')" style="background:#D1FAE5;color:#065F46;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">👍 Good</button>
        <button onclick="rateFeedback(${i},'bad')" style="background:#FEE2E2;color:#991B1B;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">❌ Needs Fix</button>
      </div>
      <div id="fbtxt${i}" style="display:none;margin-top:10px">
        <textarea id="fbinput${i}" placeholder="What should the bot have said/done differently?" style="width:100%;height:70px;border:1px solid #E2DDF0;border-radius:6px;padding:8px;font-size:12px;resize:none;outline:none"></textarea>
        <button onclick="submitFeedback(${i})" style="margin-top:6px;background:linear-gradient(135deg,#7B2FBE,#4C6EF5);color:white;border:none;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">Add as Rule</button>
      </div>
    </div>
  `).join('');
  // store entries for later use
  window._feedbackEntries = entries;
}

function rateFeedback(i, rating) {
  if (rating === 'good') {
    document.getElementById(`fb${i}`).style.opacity = '0.5';
    document.getElementById(`fbtxt${i}`).style.display = 'none';
  } else {
    document.getElementById(`fbtxt${i}`).style.display = 'block';
    document.getElementById(`fbinput${i}`).focus();
  }
}

async function submitFeedback(i) {
  const entry = window._feedbackEntries[i];
  const feedback = document.getElementById(`fbinput${i}`).value.trim();
  if (!feedback) return;
  await fetch('/api/bot-feedback', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      inbound: entry.message || '',
      bot_message: entry.bot_message || '',
      feedback, rating: 'bad'
    })
  });
  document.getElementById(`fb${i}`).style.background = '#FEF2F2';
  document.getElementById(`fbtxt${i}`).innerHTML = '<div style="color:#059669;font-size:12px;font-weight:600">✅ Rule added! Bot will avoid this in future messages.</div>';
}

// ── Test Chat ─────────────────────────────────────────────────────────────────
let tcHistory = [];

function toggleTestChat() {
  const panel = document.getElementById('testChatPanel');
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) {
    // Load properties into dropdown if not already loaded
    if (document.getElementById('tcProperty').options.length <= 1) {
      fetch('/api/properties').then(r=>r.json()).then(d=>{
        propertiesList = d.properties || [];
        refreshPropertyDropdown();
      });
    }
    setTimeout(() => document.getElementById('tcInput').focus(), 300);
  }
}

function clearTestChat() {
  tcHistory = [];
  document.getElementById('tcMessages').innerHTML = '<div style="text-align:center;color:var(--faint);font-size:12px;padding:20px">Type a message below as if you were a lead.<br>The bot will respond (no real SMS sent).</div>';
}

function tcKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTestMessage(); }
}

function tcAppend(html) {
  const msgs = document.getElementById('tcMessages');
  // Remove placeholder text on first message
  if (msgs.querySelector('div[style*="padding:20px"]')) msgs.innerHTML = '';
  msgs.insertAdjacentHTML('beforeend', html);
  msgs.scrollTop = msgs.scrollHeight;
}

async function sendTestMessage() {
  const input = document.getElementById('tcInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  document.getElementById('tcSend').disabled = true;

  tcAppend(`<div class="tc-msg-lead">${esc(msg)}</div>`);
  tcHistory.push({role: 'lead', text: msg});

  // Typing indicator
  const tid = 'tc-typing-' + Date.now();
  tcAppend(`<div class="tc-msg-bot" id="${tid}" style="color:var(--faint);font-style:italic">Bot is thinking…</div>`);

  const propSel = document.getElementById('tcProperty');
  const propAddress = propSel.value;
  const propNotes = propSel.selectedOptions[0]?.dataset.notes || '';

  try {
    const res = await fetch('/api/test-chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        stage: document.getElementById('tcStage').value,
        name: document.getElementById('tcName').value || 'Test Lead',
        property_address: propAddress,
        property_summary: propNotes,
        lock_code: document.getElementById('tcLockCode').value,
        backup_lock_code: document.getElementById('tcBackupLockCode').value,
        history: tcHistory.slice(0, -1),
      })
    });
    const d = await res.json();
    const el = document.getElementById(tid);
    if (el) el.remove();

    if (d.error) {
      tcAppend(`<div class="tc-msg-bot" style="color:var(--red)">Error: ${esc(d.error)}</div>`);
    } else if (d.action === 'skip') {
      tcAppend(`<div class="tc-msg-skip">⏭ Bot would skip — ${esc(d.reasoning||'no action needed')}</div>`);
    } else {
      const fbId = 'tcfb-' + Date.now();
      let botHtml = `<div class="tc-msg-bot" id="${fbId}">
        <div class="tc-action-badge">${al(d.action)}</div>`;
      if (d.message) {
        botHtml += `<div>${esc(d.message)}</div>`;
        tcHistory.push({role: 'bot', text: d.message});
      }
      if (d.follow_up_message) {
        botHtml += `<div style="margin-top:6px;padding-top:6px;border-top:1px dashed #C4B5FD">${esc(d.follow_up_message)}</div>`;
      }
      if (d.new_stage) botHtml += `<div class="tc-stage-change">→ ${esc(d.new_stage)}</div>`;
      if (d.reasoning) botHtml += `<div class="tc-reasoning">${esc(d.reasoning)}</div>`;
      // Feedback buttons
      botHtml += `<div style="display:flex;gap:6px;margin-top:8px;align-items:center">
        <button onclick="tcRate('${fbId}','good','${encodeURIComponent(msg)}','${encodeURIComponent(d.message||'')}')" style="background:#D1FAE5;border:none;color:#065F46;padding:2px 10px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">👍</button>
        <button onclick="tcRate('${fbId}','bad','${encodeURIComponent(msg)}','${encodeURIComponent(d.message||'')}')" style="background:#FEE2E2;border:none;color:#991B1B;padding:2px 10px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">👎 Needs fix</button>
      </div>
      <div id="${fbId}-input" style="display:none;margin-top:6px">
        <textarea placeholder="מה הבוט היה צריך לענות/לעשות אחרת?" style="width:100%;height:55px;border:1px solid var(--border);border-radius:6px;padding:6px;font-size:11px;resize:none;outline:none;font-family:inherit"></textarea>
        <button onclick="tcSubmitFeedback('${fbId}','${encodeURIComponent(msg)}','${encodeURIComponent(d.message||'')}')" style="margin-top:4px;background:var(--grad);color:white;border:none;padding:4px 14px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">Add as Rule →</button>
      </div>`;
      botHtml += '</div>';
      tcAppend(botHtml);
    }
  } catch(e) {
    const el = document.getElementById(tid);
    if (el) el.remove();
    tcAppend(`<div class="tc-msg-bot" style="color:var(--red)">Error: ${esc(e.message)}</div>`);
  }
  document.getElementById('tcSend').disabled = false;
  document.getElementById('tcInput').focus();
}

function tcRate(fbId, rating, encodedMsg, encodedBot) {
  if (rating === 'good') {
    const el = document.getElementById(fbId);
    if (el) el.style.opacity = '0.6';
  } else {
    const inp = document.getElementById(fbId + '-input');
    if (inp) { inp.style.display = 'block'; inp.querySelector('textarea')?.focus(); }
  }
}

async function tcSubmitFeedback(fbId, encodedMsg, encodedBot) {
  const inpEl = document.getElementById(fbId + '-input');
  const ta = inpEl?.querySelector('textarea');
  const feedback = ta?.value?.trim();
  if (!feedback) return;
  await fetch('/api/bot-feedback', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({inbound: decodeURIComponent(encodedMsg), bot_message: decodeURIComponent(encodedBot), feedback, rating:'bad'})
  });
  inpEl.innerHTML = '<div style="color:var(--green);font-size:11px;font-weight:600">✅ Rule added!</div>';
}

// ── Recent Interactions ───────────────────────────────────────────────────────
let riCollapsed = false;

function toggleRI() {
  riCollapsed = !riCollapsed;
  document.getElementById('riBody').style.display = riCollapsed ? 'none' : 'block';
  document.getElementById('riToggle').classList.toggle('collapsed', riCollapsed);
  document.getElementById('riHeader').classList.toggle('collapsed', riCollapsed);
}

async function loadRI() {
  try {
    const r = await fetch('/api/webhook-log');
    const d = await r.json();
    const entries = (d.recent || []).filter(e => e.status !== 'received').reverse();
    const sec = document.getElementById('riSection');
    const body = document.getElementById('riBody');
    const cnt = document.getElementById('riCount');

    cnt.textContent = entries.length;
    if (!entries.length) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';

    if (riCollapsed) return;

    body.innerHTML = entries.slice(0, 10).map((e, idx) => {
      const timeStr = e.timestamp ? new Date(e.timestamp).toLocaleTimeString('en-US', {hour:'2-digit',minute:'2-digit'}) : '';
      const stageStr = e.stage || '';
      const propStr = e.property || '';
      const leadMsg = e.message ? `<div class="ri-bubble-in">← ${esc(e.message.slice(0,100))}</div>` : '';
      const riId = `ri-${idx}`;
      const botMsgHtml = e.bot_message ? `
        <div class="ri-bubble-out" style="position:relative">
          → ${esc(e.bot_message.slice(0,100))}
          <span style="margin-left:6px;cursor:pointer;font-size:11px" onclick="riToggleFeedback('${riId}')">👎</span>
        </div>
        <div id="${riId}-fb" style="display:none;margin-top:4px;padding:6px;background:#F8F7FC;border-radius:6px">
          <textarea placeholder="מה הבוט היה צריך לעשות אחרת?" style="width:100%;height:50px;border:1px solid var(--border);border-radius:5px;padding:5px;font-size:11px;resize:none;outline:none;font-family:inherit"></textarea>
          <button onclick="riSubmitFeedback('${riId}','${encodeURIComponent(e.message||'')}','${encodeURIComponent(e.bot_message||'')}')" style="margin-top:4px;background:var(--grad);color:white;border:none;padding:3px 12px;border-radius:5px;font-size:11px;cursor:pointer;font-weight:600">Add as Rule →</button>
        </div>` : '';
      const followUp = e.bot_follow_up ? `<div class="ri-bubble-out">→ ${esc(e.bot_follow_up.slice(0,100))}</div>` : '';
      const actionBadge = e.action && e.action !== 'skip'
        ? `<span class="badge ${ac(e.action)}" style="font-size:10px">${al(e.action)}</span>` : '';

      return `<div class="ri-row" style="display:block;padding:10px 16px">
        <div style="display:grid;grid-template-columns:130px 120px 1fr;gap:8px;align-items:start">
          <div>
            <div class="ri-name">${esc(e.lead_name || e.contact_id || '?')}</div>
            <div class="ri-time">${timeStr}</div>
            ${actionBadge}
          </div>
          <div>
            ${stageStr ? `<div style="font-size:11px;font-weight:600;color:var(--muted)">${esc(stageStr)}</div>` : ''}
            ${propStr ? `<div style="font-size:10px;color:var(--faint)" title="${esc(propStr)}">${esc(propStr.slice(0,30))}${propStr.length>30?'…':''}</div>` : ''}
          </div>
          <div class="ri-msgs">
            ${leadMsg}${botMsgHtml}${followUp}
            ${!leadMsg && !e.bot_message ? `<div style="color:var(--faint);font-size:11px">${esc(e.status||'')}</div>` : ''}
          </div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

let callLogOpen = false;

function toggleCallLog() {
  callLogOpen = !callLogOpen;
  const panel = document.getElementById('callLogPanel');
  panel.style.display = 'flex';
  panel.style.transform = callLogOpen ? 'translateX(0)' : 'translateX(100%)';
  if (callLogOpen) loadCallLog();
}

async function loadCallLog() {
  const loading = document.getElementById('callLogLoading');
  const table = document.getElementById('callLogTable');
  const empty = document.getElementById('callLogEmpty');
  const rows = document.getElementById('callLogRows');
  const cnt = document.getElementById('callLogCount');

  loading.style.display = 'block';
  table.style.display = 'none';
  empty.style.display = 'none';

  try {
    const r = await fetch('/api/call-log');
    const d = await r.json();
    const calls = d.calls || [];
    cnt.textContent = calls.length;
    loading.style.display = 'none';

    if (!calls.length) { empty.style.display = 'block'; return; }

    rows.innerHTML = calls.map(c => `
      <tr style="border-bottom:1px solid var(--border);transition:background 0.15s" onmouseover="this.style.background='#f8f7fc'" onmouseout="this.style.background=''">
        <td style="padding:8px 12px;font-weight:600;color:var(--text);font-size:12px">
          ${esc(c.name)}<br>
          <span style="font-weight:400;color:var(--muted);font-size:10px">${esc(c.phone||'')} ${c.property_address ? '· '+esc(c.property_address.slice(0,30)) : ''}</span>
        </td>
        <td style="padding:8px 12px;color:var(--muted);font-size:11px;white-space:nowrap">${c.call_date||'—'}<br><span style="color:#94a3b8">${c.call_time||''}</span></td>
        <td style="padding:8px 12px;text-align:center;font-size:13px">${c.call_started === 'YES' ? '✅' : '❌'}</td>
        <td style="padding:8px 12px;color:var(--muted);font-size:11px;text-align:center">${c.duration||'—'}</td>
        <td style="padding:8px 12px">
          <span style="background:${c.result_color}22;color:${c.result_color};border:1px solid ${c.result_color}44;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap">${esc(c.result_label)}</span>
        </td>
        <td style="padding:8px 12px;color:var(--muted);font-size:11px;max-width:200px">${c.ai_summary ? `<span title="${esc(c.ai_summary)}" style="cursor:help">${esc(c.ai_summary.slice(0,100))}${c.ai_summary.length>100?'…':''}</span>` : '<span style="color:#cbd5e1">—</span>'}</td>
      </tr>`).join('');
    table.style.display = 'table';
  } catch(e) {
    loading.textContent = 'שגיאה בטעינת ה-Call Log.';
  }
}

function riToggleFeedback(riId) {
  const el = document.getElementById(riId + '-fb');
  if (el) { el.style.display = el.style.display === 'none' ? 'block' : 'none'; if (el.style.display !== 'none') el.querySelector('textarea')?.focus(); }
}

async function riSubmitFeedback(riId, encodedMsg, encodedBot) {
  const el = document.getElementById(riId + '-fb');
  const ta = el?.querySelector('textarea');
  const feedback = ta?.value?.trim();
  if (!feedback) return;
  await fetch('/api/bot-feedback', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({inbound: decodeURIComponent(encodedMsg), bot_message: decodeURIComponent(encodedBot), feedback, rating:'bad'})
  });
  el.innerHTML = '<div style="color:var(--green);font-size:11px;font-weight:600">✅ Rule added!</div>';
}

// ── Call Center ───────────────────────────────────────────────────────────────
let callCenterOpen = false;
let _ccLeads = [];

function toggleCallCenter() {
  callCenterOpen = !callCenterOpen;
  const panel = document.getElementById('callCenterPanel');
  panel.style.display = 'flex';
  panel.style.transform = callCenterOpen ? 'translateX(0)' : 'translateX(100%)';
  if (callCenterOpen) loadCallCenter();
}

async function loadCallCenter() {
  const loading = document.getElementById('callCenterLoading');
  const table = document.getElementById('callCenterTable');
  const empty = document.getElementById('callCenterEmpty');
  const rows = document.getElementById('callCenterRows');
  const cnt = document.getElementById('callCenterCount');
  const selectAll = document.getElementById('ccSelectAll');

  loading.style.display = 'block';
  table.style.display = 'none';
  empty.style.display = 'none';
  if (selectAll) selectAll.checked = false;

  try {
    const r = await fetch('/api/call-center');
    const d = await r.json();
    _ccLeads = d.leads || [];
    cnt.textContent = _ccLeads.length;
    loading.style.display = 'none';

    if (!_ccLeads.length) { empty.style.display = 'block'; return; }

    rows.innerHTML = _ccLeads.map(lead => {
      const statusInfo = ccStatusBadge(lead.last_call_status);
      const neverCalled = !lead.last_call_date;
      const summaryText = lead.ai_summary
        ? (lead.ai_summary.length > 80 ? lead.ai_summary.slice(0, 80) + '…' : lead.ai_summary)
        : '—';
      const canCall = lead.can_call;
      const skipTitle = lead.skip_reason ? ` title="${esc(lead.skip_reason)}"` : '';
      return `<tr style="border-bottom:1px solid #d1fae5;transition:background 0.15s" onmouseover="this.style.background='#f0fdf4'" onmouseout="this.style.background=''">
        <td style="padding:8px 10px;text-align:center">
          <input type="checkbox" id="cc_${lead.contact_id}" data-contact-id="${esc(lead.contact_id)}" data-name="${esc(lead.name)}" data-phone="${esc(lead.phone)}" data-stage="${esc(lead.stage)}" ${!canCall ? 'disabled' : ''} style="cursor:${canCall ? 'pointer' : 'not-allowed'};opacity:${canCall ? '1' : '0.4'}">
        </td>
        <td style="padding:8px 10px;font-weight:600;color:#065f46;font-size:12px">
          ${esc(lead.name)}<br>
          <span style="font-weight:400;color:#6b7280;font-size:10px">${esc(lead.phone||'')}</span>
        </td>
        <td style="padding:8px 10px">
          <span class="badge ${sc(lead.stage)}" style="font-size:11px">${esc(lead.stage)}</span>
        </td>
        <td style="padding:8px 10px;color:#6b7280;font-size:11px;white-space:nowrap">
          ${neverCalled
            ? '<span style="color:#94a3b8">Never</span>'
            : esc(lead.last_call_date)}
        </td>
        <td style="padding:8px 10px">
          ${neverCalled
            ? '<span style="background:#f1f5f9;color:#94a3b8;border:1px solid #e2e8f0;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">Never called</span>'
            : `<span style="background:${statusInfo.bg};color:${statusInfo.color};border:1px solid ${statusInfo.border};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">${esc(statusInfo.label)}</span>`}
        </td>
        <td style="padding:8px 10px;color:#6b7280;font-size:11px;max-width:160px">
          ${lead.ai_summary
            ? `<span title="${esc(lead.ai_summary)}" style="cursor:help">${esc(summaryText)}</span>`
            : '<span style="color:#cbd5e1">—</span>'}
        </td>
        <td style="padding:8px 10px">
          <button onclick="triggerCall('${esc(lead.contact_id)}','${esc(lead.name)}','${esc(lead.phone)}','')"
            ${!canCall ? `disabled${skipTitle}` : ''}
            style="background:${canCall ? 'linear-gradient(135deg,#059669,#0d9488)' : '#e5e7eb'};color:${canCall ? 'white' : '#9ca3af'};border:none;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;cursor:${canCall ? 'pointer' : 'not-allowed'};white-space:nowrap">
            📞 Call
          </button>
          ${!canCall && lead.skip_reason ? `<div style="font-size:10px;color:#ef4444;margin-top:3px;max-width:100px">${esc(lead.skip_reason.slice(0,40))}</div>` : ''}
        </td>
      </tr>`;
    }).join('');
    table.style.display = 'table';
  } catch(e) {
    loading.textContent = 'Error loading call center: ' + e.message;
  }
}

function ccStatusBadge(status) {
  if (!status) return {bg:'#f1f5f9', color:'#94a3b8', border:'#e2e8f0', label:'Unknown'};
  const s = status.toLowerCase();
  if (s.startsWith('booked') || s === 'success') return {bg:'#dcfce7', color:'#16a34a', border:'#86efac', label: status};
  if (s.includes('voicemail') || s.includes('no answer') || s.includes('silence') || s.includes('not_success')) return {bg:'#fef3c7', color:'#d97706', border:'#fcd34d', label: status};
  if (s.includes('not interested') || s.includes('cancelled')) return {bg:'#fee2e2', color:'#dc2626', border:'#fca5a5', label: status};
  return {bg:'#f1f5f9', color:'#64748b', border:'#e2e8f0', label: status};
}

function ccToggleAll(checked) {
  document.querySelectorAll('#callCenterRows input[type=checkbox]:not(:disabled)').forEach(cb => cb.checked = checked);
}

async function triggerCall(contactId, name, phone, propertyAddress) {
  const btn = document.querySelector(`#callCenterRows button[onclick*="${contactId}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Calling…'; }

  try {
    const r = await fetch('/api/trigger-call', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({contact_id: contactId, name, phone, property_address: propertyAddress}),
    });
    const d = await r.json();
    if (d.ok) {
      ccShowToast(`✅ Call triggered for ${name}`);
      setTimeout(() => loadCallCenter(), 1500);
    } else {
      ccShowToast(`❌ Failed: ${d.error || 'unknown error'}`, true);
      if (btn) { btn.disabled = false; btn.textContent = '📞 Call'; }
    }
  } catch(e) {
    ccShowToast(`❌ Error: ${e.message}`, true);
    if (btn) { btn.disabled = false; btn.textContent = '📞 Call'; }
  }
}

async function triggerCalls() {
  const checked = [...document.querySelectorAll('#callCenterRows input[type=checkbox]:checked')];
  if (!checked.length) { ccShowToast('No leads selected', true); return; }
  if (!confirm(`Trigger calls to ${checked.length} lead(s)?`)) return;

  for (let i = 0; i < checked.length; i++) {
    const cb = checked[i];
    const contactId = cb.dataset.contactId;
    const name = cb.dataset.name;
    const phone = cb.dataset.phone;
    await triggerCall(contactId, name, phone, '');
    if (i < checked.length - 1) await new Promise(r => setTimeout(r, 1200));
  }
}

function ccShowToast(msg, isError) {
  const t = document.getElementById('ccToast');
  t.textContent = msg;
  t.style.background = isError ? '#7f1d1d' : '#064e3b';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3500);
}

// ── Conversation Review Modal ──────────────────────────────────────────────
let _reviewLead = null;
let _reviewMsgs = [];

async function openConvReview(leadId) {
  _reviewLead = leads.find(l => l.id === leadId);
  if (!_reviewLead) return;
  const modal = document.getElementById('convReviewModal');
  document.getElementById('convReviewTitle').textContent = `${_reviewLead.name} — Loading…`;
  document.getElementById('convReviewMessages').innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted);font-size:13px">Loading messages…</div>';
  modal.style.display = 'flex';

  const res = await fetch(`/api/lead-messages/${_reviewLead.contact_id}`);
  const data = await res.json();
  const msgs = data.messages || [];
  _reviewMsgs = msgs;

  document.getElementById('convReviewTitle').textContent = `${_reviewLead.name} — Last ${msgs.length} messages`;
  const list = document.getElementById('convReviewMessages');
  if (!msgs.length) {
    list.innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted);font-size:13px">No messages found.</div>';
    return;
  }
  // msgs is newest-first from GHL; display oldest-first
  list.innerHTML = msgs.slice().reverse().map((m, i) => {
    const origIdx = msgs.length - 1 - i;
    const isBot = m.direction === 'outbound' && m.source === 'bot';
    const isHuman = m.direction === 'outbound' && m.source !== 'bot';
    const isInbound = m.direction === 'inbound';
    const bg = isInbound ? '#F0FDF4' : isBot ? '#EFF6FF' : '#FEF9C3';
    const border = isInbound ? '#10B981' : isBot ? '#3B82F6' : '#F59E0B';
    const label = isInbound ? '🙋 Lead' : isBot ? '🤖 Bot' : '👤 Team';
    return `<div style="background:${bg};border-left:3px solid ${border};border-radius:6px;padding:10px 12px;font-size:12px">
      <div style="font-size:10px;color:#6B7280;margin-bottom:4px;display:flex;justify-content:space-between">
        <span>${label}</span><span>${m.date?.slice(0,10)||''}</span>
      </div>
      <div style="color:#111;line-height:1.4">${esc(m.body)}</div>
      ${!isInbound ? `<div style="margin-top:8px">
        <button onclick="reviewMsg(${origIdx},'good')" style="background:#D1FAE5;color:#065F46;border:none;padding:3px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">👍 Good</button>
        <button onclick="reviewMsg(${origIdx},'bad')" style="background:#FEE2E2;color:#991B1B;border:none;padding:3px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600;margin-left:6px">👎 Issue</button>
        <span id="rv-status-${origIdx}" style="font-size:10px;color:var(--muted);margin-left:8px"></span>
        <div id="rv-fix-${origIdx}" style="display:none;margin-top:6px">
          <textarea id="rv-input-${origIdx}" placeholder="What's the issue? Why is this message bad? (feedback will train the bot)" style="width:100%;height:65px;border:1px solid #E2DDF0;border-radius:5px;padding:6px;font-size:11px;resize:none;outline:none"></textarea>
          <button onclick="submitConvReview(${origIdx})" style="margin-top:4px;background:linear-gradient(135deg,#7B2FBE,#4C6EF5);color:white;border:none;padding:5px 14px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">📌 Add as Bot Rule</button>
        </div>
      </div>` : ''}
    </div>`;
  }).join('');
}

function closeConvReview() {
  document.getElementById('convReviewModal').style.display = 'none';
  _reviewLead = null;
}

function reviewMsg(idx, rating) {
  if (rating === 'good') {
    document.getElementById(`rv-status-${idx}`).textContent = '✅ Noted';
    document.getElementById(`rv-fix-${idx}`).style.display = 'none';
  } else {
    document.getElementById(`rv-fix-${idx}`).style.display = 'block';
    document.getElementById(`rv-input-${idx}`).focus();
  }
}

async function submitConvReview(idx) {
  if (!_reviewLead) return;
  const msgs = _reviewMsgs;
  const msg = msgs[idx];
  // find the inbound message just before this bot message (next item, since array is newest-first)
  const prevInbound = msgs.slice(idx + 1).find(m => m.direction === 'inbound');
  const feedback = document.getElementById(`rv-input-${idx}`)?.value?.trim();
  if (!feedback) return;
  await fetch('/api/bot-feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      inbound: prevInbound?.body || '',
      bot_message: msg.body,
      feedback, rating: 'bad'
    })
  });
  document.getElementById(`rv-status-${idx}`).textContent = '✅ Rule added!';
  document.getElementById(`rv-fix-${idx}`).innerHTML = '';
}
</script>

<div id="convReviewModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:2000;align-items:center;justify-content:center" onclick="if(event.target===this)closeConvReview()">
  <div style="background:white;border-radius:14px;width:520px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,0.3)">
    <div style="background:linear-gradient(135deg,#7B2FBE,#4C6EF5);padding:14px 18px;display:flex;justify-content:space-between;align-items:center">
      <span id="convReviewTitle" style="color:white;font-weight:700;font-size:15px"></span>
      <button onclick="closeConvReview()" style="background:rgba(255,255,255,0.2);border:none;color:white;width:26px;height:26px;border-radius:50%;cursor:pointer;font-size:15px;line-height:1">✕</button>
    </div>
    <div id="convReviewMessages" style="flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px"></div>
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    missing = []
    if not GHL_API_KEY: missing.append("GHL_API_KEY")
    if not GHL_LOCATION_ID: missing.append("GHL_LOCATION_ID")
    if not OPENAI_API_KEY: missing.append("OPENAI_API_KEY")
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print("\n  TPMD Lease Agent Dashboard")
    print("  ==========================")
    print("  http://localhost:8000\n")
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
