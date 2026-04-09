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
        field_name = CUSTOM_FIELDS.get(field_id, field_id)
        result[field_name] = field.get("value", "")
    return result


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
        msg_data = await ghl_get(client, f"/conversations/{conv['id']}/messages", {"limit": 15})
        for m in msg_data.get("messages", {}).get("messages", []):
            if m.get("messageType") in ("TYPE_SMS", "TYPE_EMAIL"):
                messages.append({
                    "direction": m.get("direction", ""),
                    "body": m.get("body", "")[:200],
                    "date": m.get("dateAdded", "")[:16],
                    "type": m.get("messageType", ""),
                })

    custom = parse_custom_fields(contact.get("customFields", []))
    now = datetime.now(timezone.utc)

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
        "showing_date": custom.get("showing_date", ""),
        "application_url": custom.get("application_url", ""),
        "special_offer": custom.get("special_offer", ""),
        "property_summary": custom.get("property_summary", "")[:200],
        "recent_messages": messages[:10],
        "current_time": now.isoformat(),
    }


# ── Step 3: Ask Claude what to do ────────────────────────────────────────────

SYSTEM_PROMPT = """You are a lead management assistant for The Property Management Doctor, a property management company in Florida.

You analyze lease leads and decide the SINGLE best action to take RIGHT NOW.

RULES:
1. Never send a message if the lead has DND (do not disturb) enabled
2. Never send promotional messages to leads who were rejected for a specific reason (e.g., pet policy) — instead mark them Lost
3. If the last message was from us and was sent less than 4 hours ago, do NOT send another message (avoid spamming)
4. If the lead hasn't responded to 3+ consecutive outbound messages, space out follow-ups (wait 2+ days)
5. Always be contextual — reference the specific property, their situation, their name
6. Keep SMS messages under 160 characters when possible, max 300 characters
7. Sign messages as "Sivan" or "The Property Management Doctor team"
8. Use a warm, professional, but casual tone

STAGES & EXPECTED ACTIONS:
**CRITICAL LOGIC: If 1+ hours old AND no showing_date → TRIGGER VOICE BOT (ignore stage)**

- Inquiry (NEW LEAD):
  * < 1 hour: Send initial SMS with property details
  * 1+ hours, no showing: TRIGGER VOICE BOT (keep calling throughout day)

- Verification Auto-Sent (waiting for ID):
  * 1+ hours, no showing: TRIGGER VOICE BOT (call to book while waiting for ID)
  * If no ID after 24h: Send reminder

- Call: No Answer:
  * 1+ hours, no showing: TRIGGER VOICE BOT (call again)

- Call: Answered:
  * 1+ hours, no showing: TRIGGER VOICE BOT (book showing on the call)

- ID Rejected:
  * 1+ hours, no showing: TRIGGER VOICE BOT (book showing anyway if eligible)
  * Explain issue, ask to re-upload or disqualify
- Showing Scheduled:
  * If showing date NOT passed: No action yet
  * If showing date PASSED, no feedback yet (1+ days): Ask "How was the showing?"
  * If they say yes/interested: Send application link, move to "Application Sent"
  * If they say no/not interested: Ask what the issue was, move to "Tenant Feedback"

- Tenant Feedback:
  * If positive: Send application link
  * If negative: Acknowledge and offer alternatives or move to Lost

- Application Sent:
  * Follow up on status in 3-5 days

- Leased / Won:
  * No action needed

- Lost:
  * No action needed

HANDLING CUSTOMER REQUESTS & FEEDBACK:

**Post-Showing Feedback:**
- POSITIVE response ("I loved it!", "Yes I'm interested"):
  * Send application link immediately
  * Move to "Application Sent" stage
  * Record their positive feedback in notes
- NEGATIVE response ("Not for me", "Didn't like it", "Has issues"):
  * Ask specifically: "What was the issue? (pets, noise, layout, other?)"
  * Move to "Tenant Feedback" stage
  * **Record their feedback** in notes so we know if there's a property problem
  * Suggest alternatives if possible

**VOICE BOT TRIGGER LOGIC:**
- Trigger if: Hours Since Creation >= 1 AND showing_date is NOT scheduled (empty)
- Does NOT matter what stage they're in (Inquiry, Verification, ID Rejected, etc.)
- Action: Add tag 'call_for_showing' → GHL workflow calls them with voice bot
- Goal: Get them to commit to a showing time on the call
- Keep calling throughout day until they schedule a showing

**Reschedule Requests:**
- If "Can't make it Thursday": Offer new time OR send reschedule link
- Message: "No problem! Would {{date}} work instead? Or here's a link to pick your time: {{trigger_link.Y138P5DnMSDymQq16ycP}}"
- Update showing_date once confirmed

**Cancellations:**
- NOT automatic Lost stage
- Respond: "We understand. We'd love to find a date that works better for you. How about {{alternative_date}}?"
- Only move to Lost if they explicitly say "Not interested at all" or refuse multiple reschedule offers

**Application Sending:**
- DO NOT wait for customer to ask
- When customer expresses interest after showing: Auto-send application link
- Message: "Great! Here's your application: [send the appropriate trigger link]"
- Background check handling: Ignore anything before 2019 (unless criminal). For post-2019: "We'll verify with the property owner"

**ID Verification Reminders:**
- Use trigger link when nudging for ID upload: {{trigger_link.KQTTQsfA7QlIQs66jjbl}}
- Example: "Hi [name], to move forward we need your ID. Upload here: {{trigger_link.KQTTQsfA7QlIQs66jjbl}} (takes 1 min)"

**Property Questions:**
- Answer from: property_address, property_headline, special_offer, property_summary
- If asked about background/history: Follow background check policy above

**Trigger Links (prettier, shorter in SMS):**
- ID Verification: {{trigger_link.KQTTQsfA7QlIQs66jjbl}}
- Reschedule Tour: {{trigger_link.Y138P5DnMSDymQq16ycP}}
- Send Code/Access: {{trigger_link.5IDHUuj9VVY8x02kY2j}}

RESPOND WITH EXACTLY THIS JSON FORMAT:
{
  "action": "send_sms" | "update_stage" | "send_sms_and_update_stage" | "trigger_voice_bot" | "skip",
  "message": "SMS text here (only if sending)",
  "new_stage": "Stage Name (only if updating stage)",
  "reasoning": "Brief explanation of why this action"
}

DECISION TREE (APPLY THIS FIRST):
1. If showing_date is EMPTY AND hours_since_creation >= 1 → TRIGGER VOICE BOT (highest priority, call them)
2. Else if showing_date is EMPTY AND hours_since_creation < 1 → Send SMS with property details
3. Else if showing_date EXISTS AND date has NOT passed → Skip (wait for showing)
4. Else if showing_date EXISTS AND date HAS passed with no feedback → Ask "How was the showing?"
5. Else (customer gave feedback) → Send application link or move to Lost

ACTION GUIDE:
- "send_sms": Send an SMS message to the customer
- "update_stage": Move the opportunity to a new stage
- "send_sms_and_update_stage": Do both — send SMS and update stage
- "trigger_voice_bot": Add the 'call_for_showing' tag, which triggers GHL workflow to call the customer (for booking showings)
- "skip": No action needed right now"""


def _build_user_prompt(lead_context: dict) -> str:
    # Calculate hours since lead was created
    try:
        from datetime import datetime as dt_class
        created = dt_class.fromisoformat(lead_context['created_at'].replace('Z', '+00:00'))
        current = dt_class.fromisoformat(lead_context['current_time'].replace('Z', '+00:00'))
        hours_since_creation = (current - created).total_seconds() / 3600
    except:
        hours_since_creation = 0

    prompt = f"""Analyze this lead and decide the best action:

Lead: {lead_context['name']}
Phone: {lead_context['phone']}
Current Stage: {lead_context['stage']}
Opportunity Status: {lead_context['opp_status']}
Property: {lead_context['property_address'] or 'Not specified'}
ID Verification: {lead_context['id_status'] or 'Not done'}
Lock Code Issued: {'Yes' if lead_context['lock_code'] else 'No'}
Showing Date: {lead_context['showing_date'] or 'Not scheduled'}
Application URL: {'Sent' if lead_context['application_url'] else 'Not sent'}
Tags: {', '.join(lead_context['tags']) if lead_context['tags'] else 'None'}
DND: {lead_context['dnd']}
Special Offer: {lead_context['special_offer'] or 'None'}
Lead Created: {lead_context['created_at'][:10]}
Hours Since Creation: {hours_since_creation:.1f}h
Last Stage Change: {lead_context['last_stage_change'][:10]}
Current Time: {lead_context['current_time'][:16]} UTC

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
    """Send lead context to GPT-4o-mini (with Claude fallback) and get action recommendation."""
    user_prompt = _build_user_prompt(lead_context)

    # Try OpenAI first
    if OPENAI_API_KEY:
        try:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": 300,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return _parse_ai_response(text)
        except Exception:
            pass  # Fall through to Claude

    # Fallback to Claude Haiku
    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    return _parse_ai_response(text)


# ── Step 4: Execute actions ──────────────────────────────────────────────────

async def send_sms(client: httpx.AsyncClient, contact_id: str, message: str) -> dict:
    """Send an SMS message through GHL."""
    return await ghl_post(client, "/conversations/messages", {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
    })


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

    if action == "trigger_voice_bot":
        if dry_run:
            log_lines.append(f"  📞 [DRY RUN] Would trigger voice bot for {name}")
        else:
            await add_contact_tag(client, lead["contact_id"], "call_for_showing")
            log_lines.append(f"  📞 TRIGGERED voice bot for {name} (tag: call_for_showing)")

    if not log_lines:
        log_lines.append(f"  ❓ Unknown action '{action}' for {name}: {reasoning}")

    return "\n".join(log_lines)


# ── Main ─────────────────────────────────────────────────────────────────────

async def process_lead(client: httpx.AsyncClient, opp: dict, dry_run: bool) -> str:
    """Process a single lead: enrich → decide → act."""
    try:
        lead = await enrich_lead(client, opp)
    except Exception as e:
        name = opp.get("contact", {}).get("name", opp.get("contactId", "?"))
        return f"  ❌ ERROR enriching {name}: {e}"

    # Skip leads that are already won/lost or have DND
    if lead["opp_status"] == "lost":
        return f"  ⏭ SKIP {lead['name']} (status=lost)"
    if lead["stage"] in ("Leased / Won", "Lost"):
        return f"  ⏭ SKIP {lead['name']} (stage={lead['stage']})"
    if lead["dnd"]:
        return f"  ⏭ SKIP {lead['name']} (DND enabled)"

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
        if args.lead:
            # Process single lead
            opp = await ghl_get(client, f"/opportunities/{args.lead}")
            result = await process_lead(client, opp, dry_run)
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
            result = await process_lead(client, opp, dry_run)
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
