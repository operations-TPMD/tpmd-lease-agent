"""One-time backfill: find all contacts with AI Summary and add to call_log.json."""
import asyncio
import json
import os
import sys
from datetime import timezone
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import httpx
from lease_agent import (
    ghl_get, parse_custom_fields, get_all_opportunities,
    STAGE_MAP, CALL_LOG_FILE, GHL_API_KEY, GHL_LOCATION_ID
)

async def main():
    print("Backfilling call_log.json from contacts with AI Summary...\n")

    # Load existing log
    try:
        with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, ValueError):
        existing = []

    existing_ids = {e["contact_id"] for e in existing}
    added = []

    async with httpx.AsyncClient(timeout=30) as client:
        opps = await get_all_opportunities(client)
        print(f"Found {len(opps)} opportunities. Checking for AI Summary...\n")

        for opp in opps:
            contact_id = opp.get("contactId", "")
            contact = opp.get("contact", {})
            name = contact.get("name", contact_id)
            stage_id = opp.get("pipelineStageId", "")
            stage = STAGE_MAP.get(stage_id, "Unknown")
            created_at = opp.get("createdAt", "")

            if contact_id in existing_ids:
                continue

            try:
                contact_data = await ghl_get(client, f"/contacts/{contact_id}")
                custom_fields = contact_data.get("contact", {}).get("customFields", [])
                parsed = parse_custom_fields(custom_fields)
                ai_summary = parsed.get("ai_summary", "")
            except Exception as e:
                print(f"  ERROR {name}: {e}")
                continue

            if not ai_summary:
                continue

            entry = {
                "contact_id": contact_id,
                "name": name,
                "stage": stage,
                "triggered_at": created_at,  # best approximation for past calls
                "backfilled": True,
            }
            added.append(entry)
            existing_ids.add(contact_id)
            print(f"  ✅ {name} ({stage}) — has AI Summary")

    if added:
        all_entries = existing + added
        with open(CALL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_entries, f, ensure_ascii=False, indent=2)
        print(f"\nAdded {len(added)} entries to call_log.json")
    else:
        print("\nNo new entries found.")

if __name__ == "__main__":
    asyncio.run(main())
