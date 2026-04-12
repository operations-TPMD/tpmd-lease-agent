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

import base64
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, os.path.dirname(__file__))
from lease_agent import (
    get_all_opportunities, enrich_lead, ask_claude,
    send_sms, update_stage, add_contact_tag, STAGE_MAP, STAGE_NAME_TO_ID,
    LEASE_PIPELINE_ID, GHL_API_KEY, GHL_LOCATION_ID, OPENAI_API_KEY,
    ghl_headers, GHL_API_BASE,
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

    # Quick skip for won/lost
    if stage_name in ("Leased / Won", "Lost") or opp_status == "lost":
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

            # Find last message and last call
            last_msg, last_msg_date, last_msg_dir, last_call_date = "", "", "", ""
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
                        if mtype in ("TYPE_SMS", "TYPE_EMAIL") and not last_msg:
                            last_msg = m.get("body", "")[:200]
                            last_msg_date = m.get("dateAdded", "")[:16]
                            last_msg_dir = m.get("direction", "")
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

    contact_id = body.get("contact_id") or body.get("contactId") or body.get("contact", {}).get("id", "")
    message = body.get("message", "")

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
    <button class="btn btn-primary" id="scanBtn" onclick="startScan()">Scan All Leads</button>
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

function render() {
  let fl = leads.filter(l => !['Lost','Leased / Won'].includes(l.stage));  // Always exclude terminal stages
  if (filter === 'action') fl = fl.filter(l => !['skip','error'].includes(l.action));
  else if (filter === 'urgent') fl = fl.filter(l => (l.days_since_last_activity || 0) >= 3);
  else if (filter !== 'all') fl = fl.filter(l => l.action === filter);

  fl.sort((a,b) => {
    // Sort by: 1) days inactive (oldest first), 2) action priority
    const days_diff = (b.days_since_last_activity || 0) - (a.days_since_last_activity || 0);
    if (days_diff !== 0) return days_diff;
    const o = {send_sms_and_update_stage:0, send_sms:1, update_stage:2, call_for_showing:3, error:4, skip:5};
    return (o[a.action]??6) - (o[b.action]??6);
  });

  if (!fl.length) { document.getElementById('content').innerHTML = '<div class="center"><p style="color:#64748b">No leads match this filter</p></div>'; return; }

  let h = `<div class="tc"><table><thead><tr>
    <th style="width:40px;min-width:40px"></th>
    <th>Lead</th>
    <th>Stage</th>
    <th>Activity</th>
    <th>Action</th>
    <th>Decision</th>
    <th style="width:60px"></th>
  </tr></thead><tbody>`;

  for (const l of fl) {
    const isAct = !['skip','error'].includes(l.action);
    const cls = l.executed ? 'done' : '';

    // Lead cell
    const firstName = l.name?.split(' ')[0] || 'Unknown';
    const tags = (l.tags||[]).map(t => `<span class="tag">${t}</span>`).join('');

    // Activity cell
    const daysCls = l.days_since_last_activity >= 3 ? 'act-warn' : l.days_since_last_activity >= 1 ? 'act-val' : 'act-ok';
    const daysText = l.days_since_last_activity !== null ? `${l.days_since_last_activity}d ago` : '—';
    const dirCls = l.last_message_direction === 'inbound' ? 'dir-in' : 'dir-out';
    const dirIcon = l.last_message_direction === 'inbound' ? '← IN' : '→ OUT';
    const idBadge = l.id_status === 'verified' ? '<span class="badge s-show">ID OK</span>' : l.id_status ? `<span class="badge s-lost">${l.id_status}</span>` : '';

    // Decision cell
    let decisionHtml = '';
    if (l.message) {
      decisionHtml += `<div class="decision-msg">"${esc(l.message)}"<button class="edit-btn" onclick="openEdit('${l.id}')">edit</button></div>`;
    }
    if (l.new_stage) decisionHtml += `<div class="stage-change">→ ${l.new_stage}</div>`;
    decisionHtml += `<div class="decision-reason">${esc(l.reasoning||'')}</div>`;

    // Template selector
    if (isAct && !l.executed && l.available_templates?.length) {
      decisionHtml += `<div class="tmpl-select"><select onchange="applyTmpl('${l.id}',this.value)"><option value="">Use template...</option>`;
      for (const t of l.available_templates) {
        decisionHtml += `<option value="${t.id}">${t.name}</option>`;
      }
      decisionHtml += '</select></div>';
    }

    h += `<tr class="${cls}">
      <td style="text-align:center">
        ${isAct && !l.executed ? `<button class="chk ${l.approved?'on':''}" onclick="toggleApprove('${l.id}')" title="${l.action==='call_for_showing'?'Approve to trigger voice bot call':'Approve'}">${l.approved?'✓':''}</button>` : ''}
        ${l.action==='call_for_showing' && !l.executed ? `<div style="font-size:9px;color:#D97706;margin-top:2px">📞</div>` : ''}
      </td>
      <td>
        <div class="lead-name">${esc(l.name||'Unknown')}</div>
        <div class="lead-phone">${l.phone||''} ${l.email?'· '+l.email:''}</div>
        <div class="lead-addr">${esc(l.property_address||'')}</div>
        <div class="lead-tags">${tags} ${idBadge}</div>
      </td>
      <td><span class="badge ${sc(l.stage)}">${l.stage}</span></td>
      <td>
        <div class="act-row"><span class="act-label">Last msg:</span><span class="${daysCls}">${daysText}</span> <span class="${dirCls}" style="font-size:10px">${l.last_message_direction ? dirIcon : ''}</span></div>
        <div class="act-row"><span class="act-label">Last call:</span><span class="act-val">${l.last_call_date ? l.last_call_date.slice(0,10) : '—'}</span></div>
        <div class="act-row"><span class="act-label">Showing:</span><span class="${l.showing_date?'act-ok':'act-val'}">${l.showing_date||'—'}</span></div>
        ${l.last_message ? `<div class="msg-preview" title="${esc(l.last_message)}">${esc(l.last_message)}</div>` : ''}
      </td>
      <td><span class="badge ${ac(l.action)}">${al(l.action)}</span></td>
      <td>${decisionHtml}</td>
      <td>
        ${l.stage === 'ID Verified' || l.stage === 'Call: No Answer' ? `<button class="btn btn-voice btn-sm" onclick="triggerVoice('${l.contact_id}','${firstName}')" title="Trigger voice bot call">Call</button>` : ''}
      </td>
    </tr>`;
  }

  h += '</tbody></table></div>';
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
setInterval(loadScheduler, 30000);
</script>
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
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
