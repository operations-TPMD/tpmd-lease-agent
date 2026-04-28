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
    existing_contact_ids = {e.get("contact_id") for e in existing}
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

        # Build GHL contact lookup by phone
        print("Loading GHL opportunities for phone matching...")
        opps = await get_all_opportunities(client)
        phone_to_contact = {}
        contact_id_to_info = {}
        for opp in opps:
            contact = opp.get("contact", {})
            contact_id = opp.get("contactId", "")
            stage_id = opp.get("pipelineStageId", "")
            stage = STAGE_MAP.get(stage_id, "Unknown")
            name = contact.get("name", contact_id)
            for phone in [contact.get("phone", ""), contact.get("phone1", "")]:
                if phone:
                    normalized = phone.strip().replace(" ", "").replace("-", "")
                    if not normalized.startswith("+"):
                        normalized = "+" + normalized
                    phone_to_contact[normalized] = contact_id
            contact_id_to_info[contact_id] = {"name": name, "stage": stage}

        print(f"Loaded {len(opps)} GHL contacts\n")

        for call in vapi_calls:
            call_id = call.get("id", "")
            summary = call.get("summary", "") or call.get("analysis", {}).get("summary", "")
            started_at = call.get("startedAt", "") or call.get("createdAt", "")
            ended_at = call.get("endedAt", "")
            customer_phone = call.get("customer", {}).get("number", "")
            ended_reason = call.get("endedReason", "")

            if call_id in existing_call_ids:
                continue

            if not customer_phone:
                continue

            # Normalize phone
            phone = customer_phone.strip().replace(" ", "").replace("-", "")
            if not phone.startswith("+"):
                phone = "+" + phone

            contact_id = phone_to_contact.get(phone)
            if not contact_id:
                # Try last 10 digits match
                digits = phone[-10:]
                for p, cid in phone_to_contact.items():
                    if p.endswith(digits):
                        contact_id = cid
                        break

            if not contact_id:
                print(f"  SKIP {phone} — no matching GHL contact")
                continue

            if contact_id in existing_contact_ids:
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
            existing_contact_ids.add(contact_id)
            existing_call_ids.add(call_id)
            status = f"summary: {summary[:60]}..." if summary else f"no summary ({ended_reason})"
            print(f"  ✅ {name} ({stage}) — {status}")

    if added:
        all_entries = existing + added
        with open(CALL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_entries, f, ensure_ascii=False, indent=2)
        print(f"\nAdded {len(added)} entries to call_log.json")
    else:
        print("\nNo new entries found.")


if __name__ == "__main__":
    asyncio.run(main())
