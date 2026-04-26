"""
Lease Lead Management Agent
Scans all active leads in the GHL Lease pipeline, sends context to Claude,
and executes the recommended action (send SMS, update stage, or skip).

Usage:
    python lease_agent.py              # dry-run mode (default)
    python lease_agent.py --live       # live mode (actually sends messages)
    python lease_agent.py --lead ID    # process a single lead (dry-run)
"""

import os
import sys
import json
import asyncio
import argparse
from datetime import datetime, timezone

# Fix Windows console encoding for Unicode
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx

# ── Configuration ────────────────────────────────────────────────────────────

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_KEY = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # fallback if OpenAI unavailable

LEASE_PIPELINE_ID = "DVv60aGSOc7XtIofy4Pn"
TEAM_ALERT_PHONE = "+19545799165"

# Map stage IDs to names
STAGE_MAP = {
    "f49cd202-176e-4923-b206-dc2c1e4ab2ed": "New Lead",
    "4bfe1a63-ae59-4502-91bb-fe625c5d1306": "Verification Auto-Sent",
    "3c8c0811-0e18-4e00-bc57-d3c62a36bc5a": "Call: No Answer",
    "22b5631f-9684-4c23-b52e-2d0aaa30cf5d": "Call: Answered",
    "5ceb6abd-d164-4a3e-a97c-1612b424c54e": "Manual Review Needed",
    "eda80e2b-64e4-4239-a282-6c6d30f5b7d2": "Pending ID Review",
    "0cb27bb7-e9b4-482e-a7a8-07d9bdbe862d": "ID Rejected",
    "71ef84e3-cc59-4cf5-a25f-86f8bbffee7b": "ID Verified",
    "3f05fe34-4ea4-4082-b36f-685f969d0691": "Showing Scheduled",
    "f40de064-c6d4-4ee1-81b2-adfc58f1a22d": "Tenant Feedback",
    "75cbdb65-93db-4591-a5b3-ea6c6d3ccda8": "Application Sent",
    "44efaa6b-f463-40f5-b23a-e544a7bbb07b": "Leased / Won",
    "a3bdbf3a-68b7-4315-9672-1ed5f9fe57ac": "Lost",
}

# Reverse map for stage name → ID
STAGE_NAME_TO_ID = {v: k for k, v in STAGE_MAP.items()}

# Custom field IDs (discovered from data sampling)
CUSTOM_FIELDS = {
    "1kLJUI6WFm0Rrdz6FKdX": "property_headline",
    "3pB8lw8QtsWTn0Z5252p": "special_offer",
    "Eqc9Bqg9lkqeOAKPodKk": "property_summary",
    "VLeCogO7o4s4lGEebs49": "property_full_listing",
    "Vk9hcLmQAaoeLYYPbUUe": "property_address",
    "FJFBWSs8MHfyB7ahVlgx": "property_image_url",
    "8kn4GQoZgRG5o9RTHEM5": "id_verification_status",
    "9iMgFiYh1ohk93qHIRRx": "application_url",
    "KmC7zPPVBsaz5CDez82f": "lock_code",
    "GwjeZA0k3RaWcZ1E6oyV": "showing_date",
}


# ── GHL API helpers ──────────────────────────────────────────────────────────

def ghl_headers() -> dict:
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def ghl_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    resp = await client.get(f"{GHL_API_BASE}{path}", headers=ghl_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


async def ghl_post(client: httpx.AsyncClient, path: str, body: dict = None) -> dict:
    resp = await client.post(f"{GHL_API_BASE}{path}", headers=ghl_headers(), json=body)
    resp.raise_for_status()
    return resp.json()


# ── Step 1: Scan all open opportunities ──────────────────────────────────────

async def get_all_opportunities(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all open opportunities from the Lease pipeline."""
    all_opps = []
    start_after = None
    start_after_id = None

    while True:
        params = {
            "location_id": GHL_LOCATION_ID,
            "pipeline_id": LEASE_PIPELINE_ID,
            "limit": 100,
        }
        if start_after and start_after_id:
            params["startAfter"] = start_after
            params["startAfterId"] = start_after_id

        data = await ghl_get(client, "/opportunities/search", params)
        opps = data.get("opportunities", [])
        all_opps.extend(opps)

        meta = data.get("meta", {})
        if not meta.get("nextPageUrl") or not opps:
            break
        start_after = meta.get("startAfter")
        start_after_id = meta.get("startAfterId")

    return all_opps


# ── Step 2: Enrich each lead with contact + messages ─────────────────────────

def parse_custom_fields(custom_fields: list[dict]) -> dict:
    """Convert GHL custom fields list to a readable dict."""
    result = {}
    for field in custom_fields:
        field_id = field.get("id", "")
        value = field.get("value", "")
        if field_id in CUSTOM_FIELDS:
            result[CUSTOM_FIELDS[field_id]] = value
        else:
            # Catch AI Summary and other unmapped fields by their key name
            field_key = (field.get("fieldKey") or field.get("name") or "").lower().replace(" ", "_")
            if "ai_summary" in field_key or field_key == "contact.ai_summary":
                result["ai_summary"] = value
    return result



def _build_id_url(contact: dict, property_address: str = "") -> str:
    """Build branded short URL via tpmd.io/go redirect page."""
    contact_id = contact.get("id", "")
    return f"https://tpmd.io/go?c={contact_id}&t=id"


def _build_reschedule_url(contact: dict, property_address: str = "") -> str:
    """Build branded short URL via tpmd.io/go redirect page."""
    contact_id = contact.get("id", "")
    return f"https://tpmd.io/go?c={contact_id}&t=book"


SEND_CODE_TRIGGER_LINK_ID = "5iDHWuijJ9VVYBx02kY2"

async def _get_trigger_link_url(client, contact_id: str) -> str:
    """Resolve GHL trigger link to actual URL for a specific contact."""
    try:
        resp = await client.post(
            f"{GHL_API_BASE}/marketing/trigger-links/{SEND_CODE_TRIGGER_LINK_ID}/url",
            headers=ghl_headers(),
            json={"contactId": contact_id, "locationId": GHL_LOCATION_ID},
        )
        if resp.status_code in (200, 201):
            return resp.json().get("url", "")
    except Exception:
        pass
    # Fallback: direct URL
    return f"https://tpmd.io/verifying-access?contact_id={contact_id}"


async def enrich_lead(client: httpx.AsyncClient, opp: dict) -> dict:
    """Pull contact details and recent messages for an opportunity."""
    contact_id = opp["contactId"]
    stage_id = opp.get("pipelineStageId", "")
    stage_name = STAGE_MAP.get(stage_id, stage_id)

    # Get contact details
    contact_data = await ghl_get(client, f"/contacts/{contact_id}")
    contact = contact_data.get("contact", {})

    # Get conversations
    convos = await ghl_get(client, "/conversations/search", {
        "locationId": GHL_LOCATION_ID,
        "contactId": contact_id,
        "limit": 1,
    })

    messages = []
    for conv in convos.get("conversations", []):
        msg_data = await ghl_get(client, f"/conversations/{conv['id']}/messages", {"limit": 30})
        for m in msg_data.get("messages", {}).get("messages", []):
            if m.get("messageType") in ("TYPE_SMS", "TYPE_EMAIL"):
                messages.append({
                    "direction": m.get("direction", ""),
                    "body": m.get("body", "")[:200],
                    "date": m.get("dateAdded", "")[:16],
                    "type": m.get("messageType", ""),
                    "source": m.get("source", ""),  # "bot", "workflow", "" = human
                })

    custom = parse_custom_fields(contact.get("customFields", []))
    now = datetime.now(timezone.utc)

    # Check GHL calendar appointments for showing date
    # Only look at "Schedule your Self-Showing now" calendar
    SHOWING_CALENDAR_ID = "I27t4Z2T7ZG0SQlI3Syd"
    appts = await ghl_get(client, f"/contacts/{contact_id}/appointments")
    showing_date = ""
    best_appt = None
    now_iso = datetime.now(timezone.utc).isoformat()
    for appt in appts.get("events", []):
        if appt.get("calendarId") != SHOWING_CALENDAR_ID:
            continue
        start = appt.get("startTime", "")
        if not start:
            continue
        # Prefer the most recent appointment (upcoming first, then most recent past)
        if best_appt is None:
            best_appt = appt
        else:
            prev_start = best_appt.get("startTime", "")
            # If current is upcoming and prev is past, prefer current
            if start >= now_iso and prev_start < now_iso:
                best_appt = appt
            # If both upcoming, prefer earliest
            elif start >= now_iso and prev_start >= now_iso:
                if start < prev_start:
                    best_appt = appt
            # If both past, prefer most recent
            elif start < now_iso and prev_start < now_iso:
                if start > prev_start:
                    best_appt = appt
    showing_time = ""
    if best_appt:
        showing_date = best_appt.get("startTime", "")[:10]
        # Extract HH:MM from ISO timestamp (e.g. "2026-04-22T14:00:00+00:00")
        raw_start = best_appt.get("startTime", "")
        if "T" in raw_start:
            showing_time = raw_start[11:16]  # "HH:MM"
    # Fallback to custom field if no calendar appointment found
    if not showing_date:
        showing_date = custom.get("showing_date", "")

    return {
        "opportunity_id": opp["id"],
        "contact_id": contact_id,
        "name": contact.get("name") or opp.get("contact", {}).get("name", "Unknown"),
        "phone": contact.get("phone", ""),
        "email": contact.get("email", ""),
        "tags": contact.get("tags", []),
        "dnd": contact.get("dnd", False),
        "stage": stage_name,
        "stage_id": stage_id,
        "opp_status": opp.get("status", ""),
        "opp_name": opp.get("name", ""),
        "created_at": opp.get("createdAt", ""),
        "last_stage_change": opp.get("lastStageChangeAt", ""),
        "property_address": custom.get("property_address", ""),
        "id_status": custom.get("id_verification_status", ""),
        "lock_code": custom.get("lock_code", ""),
        "showing_date": showing_date,
        "showing_time": showing_time,
        "application_url": custom.get("application_url", ""),
        "special_offer": custom.get("special_offer", ""),
        "property_summary": custom.get("property_summary", ""),
        "property_full_listing": custom.get("property_full_listing", ""),
        "property_headline": custom.get("property_headline", ""),
        "ai_summary": custom.get("ai_summary", ""),
        "recent_messages": messages[:20],
        "current_time": now.isoformat(),
        "last_outbound_date": next(
            (m["date"][:10] for m in messages[:20] if m["direction"] == "outbound"), ""
        ),
        "id_verification_url": _build_id_url(contact, custom.get("property_address", "")),
        "reschedule_url": _build_reschedule_url(contact, custom.get("property_address", "")),
        "access_code_url": await _get_trigger_link_url(client, contact_id),
        "backup_lock_code": "",  # populated below after properties lookup
    }

    # Look up backup lockbox code from the properties list
    props_list = await get_properties_list(client)
    lead_ctx["backup_lock_code"] = _get_backup_lock_code(lead_ctx["property_address"], props_list)
    return lead_ctx


# ── Step 3: Ask Claude what to do ────────────────────────────────────────────

def _load_custom_rules(ghl_client=None) -> str:
    """Load custom rules from GHL Custom Values (persistent), fallback to local file."""
    content = ""

    # Try GHL Custom Values first (survives Railway redeploys)
    if ghl_client is None:
        # Sync fallback: try local file only
        rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_rules.txt")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
    else:
        # Caller provides async client — they must call async version instead
        pass

    if content:
        return f"\nCUSTOM RULES (defined by property manager — highest priority):\n{content}\n"
    return ""


async def _load_custom_rules_async(client) -> str:
    """Async version: reads bot rules from GHL Custom Values."""
    try:
        resp = await client.get(
            f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
            headers=ghl_headers(),
        )
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == "bot_rules":
                    content = cv.get("value", "").strip()
                    if content:
                        return f"\nCUSTOM RULES (defined by property manager — highest priority):\n{content}\n"
    except Exception:
        pass
    return _load_custom_rules()  # fallback to file


async def get_unavailable_properties(client) -> list[str]:
    """Return list of unavailable property addresses from GHL Custom Values."""
    try:
        resp = await client.get(
            f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
            headers=ghl_headers(),
        )
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == "unavailable_properties":
                    raw = cv.get("value", "").strip()
                    if raw:
                        return [line.strip().lower() for line in raw.splitlines() if line.strip()]
    except Exception:
        pass
    return []


async def get_properties_list(client) -> list[dict]:
    """Return full properties list (with backup_lock_code) from GHL Custom Values."""
    try:
        resp = await client.get(
            f"{GHL_API_BASE}/locations/{GHL_LOCATION_ID}/customValues",
            headers=ghl_headers(),
        )
        if resp.status_code == 200:
            for cv in resp.json().get("customValues", []):
                if cv.get("name") == "properties_list":
                    raw = cv.get("value", "").strip()
                    if raw:
                        return json.loads(raw)
    except Exception:
        pass
    return []


def _get_backup_lock_code(property_address: str, properties_list: list[dict]) -> str:
    """Look up the backup lockbox code for a given property address."""
    if not property_address or not properties_list:
        return ""
    addr_lower = property_address.lower().strip()
    for prop in properties_list:
        prop_addr = prop.get("address", "").lower().strip()
        if prop_addr and (addr_lower in prop_addr or prop_addr in addr_lower):
            return prop.get("backup_lock_code", "")
    return ""


def _is_property_unavailable(property_address: str, unavailable_list: list[str]) -> bool:
    """Check if a property address matches any entry in the unavailable list."""
    if not property_address or not unavailable_list:
        return False
    addr_lower = property_address.lower().strip()
    return any(addr_lower in ua or ua in addr_lower for ua in unavailable_list)


SYSTEM_PROMPT_BASE = """You are a lead management assistant for The Property Management Doctor (Florida). Decide the SINGLE best action for each lead RIGHT NOW.

RULES:
1. DND enabled → always skip
2. Max 2 proactive outbound SMS/day (ET). Instant replies to inbound are exempt.
3. 3+ unanswered outbound in a row → wait 2 days before next SMS
4. SMS max 160 chars (hard limit 300). Sign as "Sivan" or "The PMD team".
5. Never repeat content already said in the conversation. Before sending, scan ALL previous outbound messages — if the same information, link, or CTA was already sent, do NOT send it again in any form or wording. Exceptions: showing reminders (today/tomorrow) and the scheduling link if lead hasn't booked yet.
6. Answer lead questions directly from PROPERTY DETAILS before any CTA.
7. NEVER say "ID", "identity verification", or "upload ID". Use the schedule link only.
8. Direct question from lead → TWO messages: answer in "message", CTA in "follow_up_message". Proactive → one message only.
8b. CONVERSATION-FIRST messaging for Verification Auto-Sent / ID Verified / ID Rejected stages (SMS path):
    - Message 1 (first ever outbound): warm greeting + one genuine question about their interest/timeline. NO link yet.
    - Message 2 (if no reply after 1-2 days): light check-in, different angle, still no link unless they replied.
    - Message 3+ (if they replied OR after 2+ attempts): only NOW include the scheduling link, naturally woven into the message — not as a standalone CTA.
    - NEVER lead with the link. The link should feel like a next step they're being invited to, not a task they're being assigned.
    - Vary tone each time: curious → helpful → gentle nudge. Never repeat the same phrasing.
9. Days Since Showing = N/A → showing hasn't happened OR ended less than 1 hour ago. NEVER ask "how was the showing?" until at least 1 hour has passed since the showing time.
10. Lead responded today → no proactive follow-up. Respond only if they asked something.
11. Honor what the lead said (rescheduled, confirmed, never went). Don't contradict them.
12. If the last outbound message was sent by a HUMAN (not the bot) within the last 20 minutes → action must be "skip". Do not send anything while a human agent may be actively handling the lead. Check "Last Outbound Message" field.
13. Human handling window: If Last Outbound Message is "human" AND Minutes Ago < 20 → skip, regardless of any other logic.
14. Max 3 post-showing follow-up attempts. After 3 with no reply → skip forever.
15. LOCKBOX/ACCESS FAILURE — use this two-step fallback:
    Step 1: If the lead says the access code isn't working AND a "Backup Lock Code" is available AND the backup code has NOT already been sent in this conversation → send the backup code with a brief apology.
    Step 2: If the backup code was already sent and still doesn't work, OR no backup code exists, OR the lead reports failure a second time → action must be "escalate_to_team". Do NOT repeat any code that has already failed.
16. If the lead reports the same problem 2+ times with no resolution → action must be "escalate_to_team".
17. AI Summary: If "AI Summary" is provided and the lead has responded but has no showing scheduled → read the summary, address the specific concern or objection raised in it. Don't ignore the summary.
18. Voice bot calls (Verification Auto-Sent, ID Verified, ID Rejected): ONLY use action "trigger_voice_bot" for leads in these stages when ALL are true:
    - No showing_date scheduled yet
    - Last inbound message is 2+ days old (or no inbound at all, only outbound from bot)
    - Lead is not in DND
    - Do NOT call more than once per day for the same lead (check if tag "call_for_showing" was already added today)
    - AI Summary does NOT indicate the lead is not interested, not looking, or asked not to be contacted (if summary says "not interested", "no longer looking", "do not call", "wrong number", "stop" → skip or move to Lost instead)
    - If AI Summary says "voicemail", "no answer", "left message" → still eligible to call again
    - If AI Summary says "interested", "wants to schedule", "asked question" → send SMS to follow up instead of calling again
    - For ID Rejected leads, naturally bring up ID resubmission during the call conversation if needed
19. ID reminder after voice bot scheduling: If stage is "Showing Scheduled" AND id_status is "pending" or empty AND no message about ID/verification has been sent recently → send a gentle reminder that they need to complete verification to receive the property access code.

{custom_rules}

STAGE ACTIONS:
- New Lead / Call: No Answer / Call: Answered → if no showing_date: send SMS to book showing.
- Verification Auto-Sent / ID Verified / ID Rejected → if no showing_date AND last inbound is 2+ days ago (or never) AND not DND AND AI Summary doesn't show disinterest → action must be "trigger_voice_bot". If AI Summary shows interest/question → send SMS. If AI Summary shows not interested → skip or Lost.
- Showing Scheduled (not yet passed): TODAY or TOMORROW → reminder with address + lock code. 2+ days away → skip. If id_status is pending/empty → add ID reminder.
- After Showing (Days Since Showing >= 0):
  * 0 attempts: "How was the showing?" warm, no application link yet
  * 1 attempt, no reply, 2+ days: different check-in
  * 2 attempts, no reply: final nudge + application link, move to Application Sent
  * 3+ attempts no reply: skip
  * Positive reply → application link, move to "Application Sent"
  * Negative reply → ask what issue, move to "Tenant Feedback"
  * Said never went / rescheduled → help reschedule, don't ask about showing
- Tenant Feedback: address specific concern from PROPERTY DETAILS. If resolved → app link. If done → Lost.
- Application Sent: SKIP entirely — do not process or message leads in this stage. The bot does not follow up after the application link is sent.

STAGE MOVEMENT — always use send_sms_and_update_stage when moving:
- Lead confirms showing date/time → "Showing Scheduled" + set appointment_date/time
- Lead positive after showing OR application link sent → "Application Sent"
- Lead explicitly not interested → "Lost"
- Lead had concerns after showing → "Tenant Feedback"

LINKS:
- Schedule Showing: use "Schedule Showing Link" field
- Reschedule: use "Reschedule Link" field
- Access Code (when lead is at door): use "access_code_url" field — this triggers the workflow to generate the unique code
- Application: {{ contact.application_link }}

RESPOND WITH EXACTLY THIS JSON:
{
  "action": "send_sms" | "update_stage" | "send_sms_and_update_stage" | "create_appointment" | "escalate_to_team" | "trigger_voice_bot" | "skip",
  "message": "SMS text to lead (for escalate_to_team: apologetic message to lead saying team will call them shortly)",
  "follow_up_message": "second SMS or empty string",
  "new_stage": "Stage Name or empty string",
  "appointment_date": "YYYY-MM-DD or empty string",
  "appointment_time": "HH:MM or empty string",
  "escalation_reason": "short description of why escalating (only for escalate_to_team)",
  "reasoning": "one sentence"
}"""


def _build_user_prompt(lead_context: dict) -> str:
    # Calculate hours since lead was created
    try:
        from datetime import datetime as dt_class
        created = dt_class.fromisoformat(lead_context['created_at'].replace('Z', '+00:00'))
        current = dt_class.fromisoformat(lead_context['current_time'].replace('Z', '+00:00'))
        hours_since_creation = (current - created).total_seconds() / 3600
    except:
        hours_since_creation = 0

    # Find last outbound message date
    last_outbound_date = ""
    for msg in lead_context.get("recent_messages", []):
        if msg["direction"] == "outbound":
            last_outbound_date = msg["date"][:10]
            break

    # Find last inbound message (unanswered?)
    last_inbound_date = ""
    last_inbound_body = ""
    for msg in lead_context.get("recent_messages", []):
        if msg["direction"] == "inbound":
            last_inbound_date = msg["date"][:10]
            last_inbound_body = msg["body"][:100]
            break

    current_date_et = lead_context['current_time'][:10]
    # Count proactive outbound messages sent today (for 2-per-day limit)
    outbound_today_count = sum(
        1 for msg in lead_context.get("recent_messages", [])
        if msg["direction"] == "outbound" and msg["date"][:10] == current_date_et
    )

    # Count consecutive unanswered outbound (newest first) — hard enforce Rule 3
    consecutive_unanswered = 0
    for msg in lead_context.get("recent_messages", []):
        if msg["direction"] == "outbound":
            consecutive_unanswered += 1
        elif msg["direction"] == "inbound":
            break  # inbound message breaks the streak

    # Calculate days/hours since/until showing
    showing_date_str = lead_context.get("showing_date", "")
    showing_time_str = lead_context.get("showing_time", "")  # HH:MM if available
    days_since_showing = None
    days_until_showing = None
    hours_since_showing = None
    if showing_date_str:
        try:
            from datetime import datetime as dt_class, date as date_class
            import zoneinfo
            ET = zoneinfo.ZoneInfo("America/New_York")
            # Build showing datetime (use time if available, else assume end of day)
            if showing_time_str:
                showing_dt = dt_class.strptime(f"{showing_date_str[:10]} {showing_time_str[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            else:
                # No time known — assume showing ends at 8pm ET
                showing_dt = dt_class.strptime(f"{showing_date_str[:10]} 20:00", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            now_et = dt_class.fromisoformat(lead_context['current_time'].replace('Z', '+00:00')).astimezone(ET)
            diff_hours = (now_et - showing_dt).total_seconds() / 3600
            if diff_hours >= 1:  # Only consider showing "done" after 1 hour
                days_since_showing = int(diff_hours // 24)
                hours_since_showing = round(diff_hours, 1)
            elif diff_hours < 0:
                days_until_showing = int(abs(diff_hours) // 24)
            # else: showing is within the last hour — treat as still ongoing
        except:
            # Fallback to date-only comparison
            from datetime import date as date_class
            showing = date_class.fromisoformat(showing_date_str[:10])
            today = date_class.fromisoformat(current_date_et)
            diff = (today - showing).days
            if diff > 0:
                days_since_showing = diff
            elif diff < 0:
                days_until_showing = abs(diff)

    # Count post-showing outbound messages (to enforce 3-message cap)
    post_showing_outbound_count = 0
    if days_since_showing is not None and showing_date_str:
        for msg in lead_context.get("recent_messages", []):
            if msg["direction"] == "outbound" and msg["date"][:10] >= showing_date_str[:10]:
                post_showing_outbound_count += 1

    # Check if last inbound was today (lead already responded today)
    last_inbound_today = last_inbound_date == current_date_et if last_inbound_date else False

    # Calculate days since last inbound message
    days_since_inbound = None
    if last_inbound_date:
        try:
            from datetime import date as date_class
            last_inbound_d = date_class.fromisoformat(last_inbound_date)
            today_d = date_class.fromisoformat(current_date_et)
            days_since_inbound = (today_d - last_inbound_d).days
        except:
            pass

    # Detect last outbound message (bot or human) and how many minutes ago
    last_outbound_minutes_ago = None
    last_outbound_source = None
    now_dt = datetime.fromisoformat(lead_context['current_time'].replace('Z', '+00:00'))
    for msg in lead_context.get("recent_messages", []):
        if msg["direction"] == "outbound":
            source = msg.get("source", "")
            # source: "" or missing = human, "bot"/"workflow"/"automated" = bot
            is_human = source not in ("bot", "automated", "workflow")
            try:
                msg_dt = datetime.fromisoformat(msg["date"].replace('Z', '+00:00'))
                diff_minutes = (now_dt - msg_dt).total_seconds() / 60
                last_outbound_minutes_ago = round(diff_minutes)
                last_outbound_source = "human" if is_human else "bot"
            except Exception:
                pass
            break

    # Only include full listing if lead asked a property question — saves tokens
    QUESTION_KEYWORDS = ("rent", "price", "cost", "pet", "dog", "cat", "fee", "util", "park",
                         "avail", "bedroom", "bath", "sqft", "size", "where", "located", "how much",
                         "laundry", "garage", "deposit", "include", "allow", "accept")
    inbound_lower = last_inbound_body.lower()
    has_question = any(kw in inbound_lower for kw in QUESTION_KEYWORDS)
    if has_question:
        property_info = lead_context.get("property_full_listing") or lead_context.get("property_summary") or ""
    else:
        property_info = lead_context.get("property_summary") or ""

    prompt = f"""Analyze this lead and decide the best action:

Lead: {lead_context['name']}
Phone: {lead_context['phone']}
Current Stage: {lead_context['stage']}
Opportunity Status: {lead_context['opp_status']}
Property: {lead_context['property_address'] or 'Not specified'}
ID Verification: {lead_context['id_status'] or 'Not done'}
Lock Code (live): {lead_context['lock_code'] if lead_context['lock_code'] else 'Not issued'}
Backup Lock Code: {lead_context['backup_lock_code'] if lead_context['backup_lock_code'] else 'None set'}
AI Summary: {lead_context['ai_summary'] if lead_context['ai_summary'] else 'None'}
Showing Date: {lead_context['showing_date'] or 'Not scheduled'}
Days Until Showing: {days_until_showing if days_until_showing is not None else 'N/A'}
Days Since Showing: {days_since_showing if days_since_showing is not None else 'N/A (showing has not happened yet or ended less than 1 hour ago)'}
Hours Since Showing: {hours_since_showing if hours_since_showing is not None else 'N/A'}
Post-Showing Outbound Messages Sent: {post_showing_outbound_count}
Lead Responded Today: {last_inbound_today}
Last Outbound Message: {last_outbound_source} ({last_outbound_minutes_ago} min ago if last_outbound_minutes_ago is not None else 'N/A')
Application URL: {lead_context['application_url'] if lead_context['application_url'] else 'Not available'}
Schedule Showing Link: {lead_context['id_verification_url']}
Reschedule Link: {lead_context['reschedule_url']}
Tags: {', '.join(lead_context['tags']) if lead_context['tags'] else 'None'}
DND: {lead_context['dnd']}
Special Offer: {lead_context['special_offer'] or 'None'}
Lead Created: {lead_context['created_at'][:10]}
Hours Since Creation: {hours_since_creation:.1f}h
Last Stage Change: {lead_context['last_stage_change'][:10]}
Last Outbound SMS: {last_outbound_date or 'Never'}
Last Inbound Message: {f"{last_inbound_date}: {last_inbound_body}" if last_inbound_date else 'None'}
Days Since Last Inbound (or Creation if Never): {days_since_inbound if days_since_inbound is not None else 'N/A'}
Outbound SMS Sent Today: {outbound_today_count}
Consecutive Unanswered Outbound (in a row, no reply): {consecutive_unanswered}
Current Date (ET): {current_date_et}

PROPERTY DETAILS (use this to answer any questions about rent, utilities, pets, fees, availability, etc.):
{property_info if property_info else '(No property details available)'}

IMPORTANT: If Outbound SMS Sent Today >= 2 → action must be "skip" (Rule 3: max 2 proactive messages per lead per day). Instant replies to inbound messages are exempt from this limit.
IMPORTANT: If Lead Responded Today = True → do NOT send a proactive follow-up. Only respond if they asked a specific question.
IMPORTANT: If Days Since Showing = N/A → the showing has NOT happened yet. NEVER ask "how was the showing?" — it hasn't occurred.
IMPORTANT: If Post-Showing Outbound Messages Sent >= 3 and no inbound response → action must be "skip". Stop messaging about the showing.
IMPORTANT: If Last Inbound Message exists and is UNANSWERED → respond to it first before any scheduled follow-up.
IMPORTANT: If the lead asked a specific question, answer it directly using the PROPERTY DETAILS above — do not ignore their question.

Recent messages (newest first):
"""
    for msg in lead_context["recent_messages"]:
        direction = "US→LEAD" if msg["direction"] == "outbound" else "LEAD→US"
        prompt += f"  [{direction}] {msg['date']}: {msg['body']}\n"
    if not lead_context["recent_messages"]:
        prompt += "  (No messages yet)\n"
    return prompt


def _parse_ai_response(text: str) -> dict:
    """Parse JSON from AI response, handling markdown code blocks."""
    try:
        if "```" in text:
            text = text.split("```json")[-1].split("```")[0]
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {"action": "skip", "reasoning": f"Failed to parse AI response: {text[:100]}"}


async def ask_claude(client: httpx.AsyncClient, lead_context: dict) -> dict:
    """Send lead context to GPT-4o-mini and get action recommendation."""
    user_prompt = _build_user_prompt(lead_context)
    custom_rules = await _load_custom_rules_async(client)
    system_prompt = SYSTEM_PROMPT_BASE.replace("{custom_rules}", custom_rules)

    # Use OpenAI (GPT-4o-mini)
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "max_tokens": 400,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:400]}")
    text = resp.json()["choices"][0]["message"]["content"]
    return _parse_ai_response(text)


# ── Step 4: Execute actions ──────────────────────────────────────────────────

async def create_appointment(client: httpx.AsyncClient, contact_id: str, date_str: str, time_str: str) -> dict:
    """Create a GHL appointment (calendar event) for a showing."""
    try:
        # Combine date + time into ISO format (assume ET timezone)
        from datetime import datetime as dt_cls
        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")
        naive = dt_cls.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        start_dt = naive.replace(tzinfo=ET)
        end_dt = start_dt.replace(hour=start_dt.hour + 1)  # 1-hour slot

        resp = await client.post(
            f"{GHL_API_BASE}/calendars/events",
            headers=ghl_headers(),
            json={
                "locationId": GHL_LOCATION_ID,
                "contactId": contact_id,
                "title": "Property Showing",
                "startTime": start_dt.isoformat(),
                "endTime": end_dt.isoformat(),
                "status": "confirmed",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


async def send_team_alert(client: httpx.AsyncClient, lead: dict, reason: str) -> bool:
    """Send an urgent SMS alert to the team phone number via GHL."""
    import logging
    _log = logging.getLogger("team_alert")
    name = lead.get("name", "Unknown")
    phone = lead.get("phone", "")
    prop = lead.get("property_address", "")
    msg = (
        f"🚨 URGENT — {name} ({phone}) needs help at {prop}.\n"
        f"Reason: {reason}\n"
        f"Please call or text them NOW."
    )
    try:
        # Find or create a conversation with the team phone number
        resp = await client.post(
            f"{GHL_API_BASE}/conversations/messages/outbound",
            headers=ghl_headers(),
            json={
                "type": "SMS",
                "phone": TEAM_ALERT_PHONE,
                "message": msg,
                "locationId": GHL_LOCATION_ID,
            },
        )
        if resp.status_code in (200, 201):
            _log.info(f"Team alert sent for {name}: {reason}")
            return True
        _log.error(f"Team alert failed: {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        _log.error(f"Team alert exception: {e}")
        return False


async def send_sms(client: httpx.AsyncClient, contact_id: str, message: str) -> dict:
    """Send an SMS message through GHL."""
    import logging
    _sms_log = logging.getLogger("send_sms")
    try:
        resp = await client.post(
            f"{GHL_API_BASE}/conversations/messages",
            headers=ghl_headers(),
            json={"type": "SMS", "contactId": contact_id, "message": message},
        )
        if resp.status_code not in (200, 201):
            _sms_log.error(
                f"SMS failed for contact {contact_id} | status={resp.status_code} | "
                f"message_preview={message[:80]!r} | ghl_response={resp.text[:300]}"
            )
            resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _sms_log.error(f"SMS exception for contact {contact_id}: {e}")
        raise


async def update_stage(client: httpx.AsyncClient, opportunity_id: str, new_stage_name: str) -> dict:
    """Update an opportunity's pipeline stage."""
    stage_id = STAGE_NAME_TO_ID.get(new_stage_name)
    if not stage_id:
        return {"error": f"Unknown stage: {new_stage_name}"}

    resp = await client.put(
        f"{GHL_API_BASE}/opportunities/{opportunity_id}",
        headers=ghl_headers(),
        json={
            "pipelineId": LEASE_PIPELINE_ID,
            "pipelineStageId": stage_id,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def add_contact_tag(client: httpx.AsyncClient, contact_id: str, tag: str) -> dict:
    """Add a tag to a contact (triggers GHL workflows)."""
    return await ghl_post(client, f"/contacts/{contact_id}/tags", {
        "tags": [tag],
    })


CALL_LOG_FILE = os.path.join(os.path.dirname(__file__), "call_log.json")

def _append_call_log(lead: dict):
    """Persist a call trigger event to call_log.json."""
    import json as _json
    try:
        try:
            with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
                entries = _json.load(f)
        except (FileNotFoundError, ValueError):
            entries = []
        entries.append({
            "contact_id": lead.get("contact_id", ""),
            "name": lead.get("name", ""),
            "stage": lead.get("stage", ""),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        })
        with open(CALL_LOG_FILE, "w", encoding="utf-8") as f:
            _json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def execute_action(
    client: httpx.AsyncClient, lead: dict, decision: dict, dry_run: bool
) -> str:
    """Execute the decided action. Returns a log string."""
    action = decision.get("action", "skip")
    name = lead["name"]
    reasoning = decision.get("reasoning", "")

    if action == "skip":
        return f"  ⏭ SKIP {name} ({lead['stage']}): {reasoning}"

    log_lines = []

    if action in ("send_sms", "send_sms_and_update_stage"):
        msg = decision.get("message", "")
        if dry_run:
            log_lines.append(f"  📱 [DRY RUN] Would SMS {name}: {msg}")
        else:
            await send_sms(client, lead["contact_id"], msg)
            log_lines.append(f"  📱 SENT SMS to {name}: {msg}")

    if action in ("update_stage", "send_sms_and_update_stage"):
        new_stage = decision.get("new_stage", "")
        if dry_run:
            log_lines.append(f"  🔄 [DRY RUN] Would move {name}: {lead['stage']} → {new_stage}")
        else:
            await update_stage(client, lead["opportunity_id"], new_stage)
            log_lines.append(f"  🔄 MOVED {name}: {lead['stage']} → {new_stage}")

    if action == "create_appointment":
        appt_date = decision.get("appointment_date", "")
        appt_time = decision.get("appointment_time", "09:00")
        if dry_run:
            log_lines.append(f"  📅 [DRY RUN] Would create appointment for {name} on {appt_date} {appt_time}")
        elif appt_date:
            result = await create_appointment(client, lead["contact_id"], appt_date, appt_time)
            if "error" in result:
                log_lines.append(f"  ❌ Appointment failed for {name}: {result['error']}")
            else:
                log_lines.append(f"  📅 APPOINTMENT created for {name}: {appt_date} {appt_time}")

    if action == "escalate_to_team":
        reason = decision.get("escalation_reason", "Lead needs urgent assistance")
        msg = decision.get("message", "I'm sorry for the trouble! Our team will call you shortly to help.")
        if dry_run:
            log_lines.append(f"  🚨 [DRY RUN] Would escalate {name} to team: {reason}")
        else:
            # SMS lead with apology
            await send_sms(client, lead["contact_id"], msg)
            # Alert team
            alerted = await send_team_alert(client, lead, reason)
            log_lines.append(f"  🚨 ESCALATED {name} to team (alert_sent={alerted}): {reason}")
        return "\n".join(log_lines)

    if action == "trigger_voice_bot":
        if dry_run:
            log_lines.append(f"  📞 [DRY RUN] Would trigger voice bot for {name}")
        else:
            await add_contact_tag(client, lead["contact_id"], "call_for_showing")
            _append_call_log(lead)
            log_lines.append(f"  📞 TRIGGERED voice bot for {name} (tag: call_for_showing)")

    if not log_lines:
        log_lines.append(f"  ❓ Unknown action '{action}' for {name}: {reasoning}")

    return "\n".join(log_lines)


# ── Main ─────────────────────────────────────────────────────────────────────

async def process_lead(client: httpx.AsyncClient, opp: dict, dry_run: bool, unavailable_properties: list[str] = None) -> str:
    """Process a single lead: enrich → decide → act."""
    # Early exit before enriching (saves API calls)
    stage_id = opp.get("pipelineStageId", "")
    stage_name = STAGE_MAP.get(stage_id, "")
    if stage_name in ("Leased / Won", "Lost") or opp.get("status") == "lost":
        name = opp.get("contact", {}).get("name", opp.get("contactId", "?"))
        return f"  ⏭ SKIP {name} (terminal stage)"

    try:
        lead = await enrich_lead(client, opp)
    except Exception as e:
        name = opp.get("contact", {}).get("name", opp.get("contactId", "?"))
        return f"  ❌ ERROR enriching {name}: {e}"

    if lead["dnd"]:
        return f"  ⏭ SKIP {lead['name']} (DND enabled)"

    # Check if property is unavailable
    if unavailable_properties is None:
        unavailable_properties = await get_unavailable_properties(client)

    if _is_property_unavailable(lead.get("property_address", ""), unavailable_properties):
        name = lead["name"]
        addr = lead.get("property_address", "")
        if dry_run:
            return f"  🚫 [DRY RUN] {name}: property unavailable ({addr}) → would send notice + move to Lost"
        # Send one final message and move to Lost
        msg = f"Hi {lead['name'].split()[0]}! Unfortunately this unit is no longer available. We'll reach out if something similar comes up. Thanks!"
        try:
            await send_sms(client, lead["contact_id"], msg)
            await update_stage(client, lead["opportunity_id"], "Lost")
        except Exception as e:
            return f"  ❌ ERROR closing unavailable lead {name}: {e}"
        return f"  🚫 {name}: property unavailable → notified + moved to Lost"

    # Hard enforce Rule 3: 3+ consecutive unanswered outbound → skip (wait 2 days)
    consecutive = 0
    for msg in lead.get("recent_messages", []):
        if msg["direction"] == "outbound":
            consecutive += 1
        elif msg["direction"] == "inbound":
            break
    if consecutive >= 3:
        last_out = lead.get("last_outbound_date", "")
        try:
            from datetime import date as _date
            days_since_last = (_date.fromisoformat(lead['current_time'][:10]) - _date.fromisoformat(last_out)).days if last_out else 99
        except:
            days_since_last = 99
        if days_since_last < 2:
            return f"  ⏭ SKIP {lead['name']} ({consecutive} unanswered outbound, last sent {days_since_last}d ago — waiting 2 days)"

    try:
        decision = await ask_claude(client, lead)
    except Exception as e:
        return f"  ❌ ERROR deciding for {lead['name']}: {e}"

    return await execute_action(client, lead, decision, dry_run)


async def main():
    parser = argparse.ArgumentParser(description="Lease Lead Management Agent")
    parser.add_argument("--live", action="store_true", help="Actually send messages (default: dry run)")
    parser.add_argument("--lead", type=str, help="Process a single opportunity ID")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of leads to process (0=all)")
    args = parser.parse_args()

    dry_run = not args.live

    if not GHL_API_KEY:
        print("ERROR: Set GHL_API_KEY environment variable")
        sys.exit(1)
    if not GHL_LOCATION_ID:
        print("ERROR: Set GHL_LOCATION_ID environment variable")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        print("Get one at https://console.anthropic.com/settings/keys")
        sys.exit(1)

    mode = "🔴 LIVE" if not dry_run else "🟡 DRY RUN"
    print(f"\n{'='*60}")
    print(f"Lease Agent — {mode} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=30) as client:
        # Load unavailable properties once (shared across all leads)
        unavailable = await get_unavailable_properties(client)
        if unavailable:
            print(f"Unavailable properties: {len(unavailable)} entries\n")

        if args.lead:
            # Process single lead
            opp = await ghl_get(client, f"/opportunities/{args.lead}")
            result = await process_lead(client, opp, dry_run, unavailable)
            print(result)
            return

        # Scan all opportunities
        print("Scanning Lease pipeline...")
        opps = await get_all_opportunities(client)
        print(f"Found {len(opps)} opportunities\n")

        if args.limit:
            opps = opps[:args.limit]

        actions_taken = 0
        skipped = 0
        errors = 0

        for opp in opps:
            result = await process_lead(client, opp, dry_run, unavailable)
            print(result)
            if "SKIP" in result:
                skipped += 1
            elif "ERROR" in result:
                errors += 1
            else:
                actions_taken += 1

        print(f"\n{'='*60}")
        print(f"Summary: {actions_taken} actions | {skipped} skipped | {errors} errors")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
