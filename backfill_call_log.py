"""One-time backfill: fetch all historical calls from VAPI and add to call_log.json."""
import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import httpx
from lease_agent import (
    ghl_get, get_all_opportunities,
    STAGE_MAP, CALL_LOG_FILE, GHL_LOCATION_ID
)

VAPI_API_KEY = "e1780850-7e60-400a-aaf4-de6662fa8d54"
VAPI_BASE = "https://api.vapi.ai"


async def fetch_all_vapi_calls(client: httpx.AsyncClient) -> list:
    """Fetch all calls from VAPI with pagination."""
    calls = []
    params = {"limit": 100}
    while True:
        resp = await client.get(
            f"{VAPI_BASE}/call",
            headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data if isinstance(data, list) else data.get("calls", data.get("data", []))
        if not batch:
            break
        calls.extend(batch)
        if len(batch) < 100:
            break
        last = batch[-1]
        params["createdAtLt"] = last.get("createdAt", "")
        if not params["createdAtLt"]:
            break
    return calls


async def main():
    print("Backfilling call_log.json from VAPI call history...\n")

    # Load existing log
    try:
        with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, ValueError):
        existing = []

    existing_call_ids = {e.get("vapi_call_id") for e in existing if e.get("vapi_call_id")}
    added = []

    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch all VAPI calls
        print("Fetching calls from VAPI...")
        try:
            vapi_calls = await fetch_all_vapi_calls(client)
        except Exception as e:
            print(f"ERROR fetching VAPI calls: {e}")
            return

        print(f"Found {len(vapi_calls)} VAPI calls\n")

        # Build GHL contact lookup by phone — fetch full contact data for each opp
        print("Loading GHL opportunities and fetching full contact details...")
        opps = await get_all_opportunities(client)
        phone_to_contact = {}
        contact_id_to_info = {}
        for i, opp in enumerate(opps):
            contact_id = opp.get("contactId", "")
            stage_id = opp.get("pipelineStageId", "")
            stage = STAGE_MAP.get(stage_id, "Unknown")
            name = opp.get("contact", {}).get("name", contact_id)
            contact_id_to_info[contact_id] = {"name": name, "stage": stage}

            try:
                contact_data = await ghl_get(client, f"/contacts/{contact_id}")
                full_contact = contact_data.get("contact", {})
                name = full_contact.get("name", name)
                contact_id_to_info[contact_id]["name"] = name
                # GHL stores phone in "phone" field
                raw_phone = full_contact.get("phone", "")
                if raw_phone:
                    normalized = raw_phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
                    if not normalized.startswith("+"):
                        normalized = "+1" + normalized.lstrip("1")
                    phone_to_contact[normalized] = contact_id
                    # Also store last-10-digits key for fallback
                    phone_to_contact[normalized[-10:]] = contact_id
            except Exception as e:
                print(f"  WARN: could not fetch contact {contact_id}: {e}")

            if (i + 1) % 20 == 0:
                print(f"  Loaded {i+1}/{len(opps)} contacts...")

        print(f"Loaded {len(opps)} GHL contacts, built {len(phone_to_contact)} phone entries\n")

        no_phone_count = 0
        already_logged = 0
        for call in vapi_calls:
            call_id = call.get("id", "")
            summary = call.get("summary", "") or call.get("analysis", {}).get("summary", "")
            started_at = call.get("startedAt", "") or call.get("createdAt", "")
            ended_at = call.get("endedAt", "")
            customer_phone = call.get("customer", {}).get("number", "")
            ended_reason = call.get("endedReason", "")

            if call_id in existing_call_ids:
                already_logged += 1
                continue

            if not customer_phone:
                no_phone_count += 1
                continue

            # Normalize phone
            phone = customer_phone.strip().replace(" ", "").replace("-", "")
            if not phone.startswith("+"):
                phone = "+" + phone

            contact_id = phone_to_contact.get(phone) or phone_to_contact.get(phone[-10:])

            if not contact_id:
                print(f"  SKIP {phone} — no matching GHL contact")
                continue

            info = contact_id_to_info.get(contact_id, {})
            name = info.get("name", contact_id)
            stage = info.get("stage", "Unknown")

            entry = {
                "contact_id": contact_id,
                "vapi_call_id": call_id,
                "name": name,
                "stage": stage,
                "triggered_at": started_at,
                "ended_at": ended_at,
                "ended_reason": ended_reason,
                "ai_summary": summary,
                "backfilled": True,
            }
            added.append(entry)
            existing_call_ids.add(call_id)
            status = f"summary: {summary[:60]}..." if summary else f"no summary ({ended_reason})"
            print(f"  ✅ {name} ({stage}) — {status}")

    print(f"\nStats: {already_logged} already in log, {no_phone_count} had no phone number")
    if added:
        all_entries = existing + added
        with open(CALL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_entries, f, ensure_ascii=False, indent=2)
        print(f"\nAdded {len(added)} entries to call_log.json")
    else:
        print("\nNo new entries found.")


if __name__ == "__main__":
    asyncio.run(main())
