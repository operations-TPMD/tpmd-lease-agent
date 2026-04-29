"""Backfill call_log.json from VAPI v2 API with full call data."""
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
VAPI_PHONE_NUMBER_ID = "0814d623-960d-4fb5-a563-762fe0b63fc1"
VAPI_BASE = "https://api.vapi.ai"

SO_CALL_SUMMARY_ID   = "db31e82f-c498-443f-9e08-5cd2e43b3113"
SO_SUCCESS_EVAL_ID   = "d45f09c9-847c-4bd5-8cd5-7ba63b3df883"
SO_APPT_BOOKED_ID    = "4030f8dc-4f0c-452c-a422-b6e5157e0c68"
SO_APPT_CANCELLED_ID = "5da34a47-1843-4a3a-8e1e-fc69d92f3bb5"
SO_APPT_RESCHEDULED_ID = "8708977d-98ea-4f4b-ab96-a6ef4a719a20"
SO_APPT_DATETIME_ID  = "220485af-d137-4df7-827b-54814e604322"


def normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not p.startswith("+"):
        p = "+1" + p.lstrip("1")
    return p


def parse_outcome(so: dict) -> str:
    if not so:
        return ""
    booked     = so.get(SO_APPT_BOOKED_ID, {}).get("result")
    cancelled  = so.get(SO_APPT_CANCELLED_ID, {}).get("result")
    rescheduled = so.get(SO_APPT_RESCHEDULED_ID, {}).get("result")
    success    = so.get(SO_SUCCESS_EVAL_ID, {}).get("result")
    appt_dt    = so.get(SO_APPT_DATETIME_ID, {}).get("result", "")
    if booked is True:
        return f"BOOKED: {appt_dt}" if appt_dt else "BOOKED"
    if cancelled is True:
        return "CANCELLED"
    if rescheduled is True:
        return f"RESCHEDULED: {appt_dt}" if appt_dt else "RESCHEDULED"
    if success is True:
        return "SUCCESS"
    if success is False:
        return "NOT_SUCCESS"
    return ""


async def fetch_all_vapi_calls(client: httpx.AsyncClient) -> list:
    """Fetch ALL calls from VAPI v2 API using proper pagination."""
    calls = []
    page = 1
    limit = 100
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}

    while True:
        params = {
            "page": page,
            "limit": limit,
            "sortOrder": "DESC",
            "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        }
        resp = await client.get(f"{VAPI_BASE}/v2/call", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("results", [])
        if not batch:
            break
        calls.extend(batch)

        meta = data.get("metadata", {})
        total = meta.get("totalItems", 0)
        per_page = meta.get("itemsPerPage", limit)
        current = meta.get("currentPage", page)
        print(f"  Page {current}: got {len(batch)} calls (running total: {len(calls)}/{total})")

        if current * per_page >= total:
            break
        page += 1

    return calls


async def main():
    print("Backfilling call_log.json from VAPI v2 API...\n")

    # Load existing log
    try:
        with open(CALL_LOG_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, ValueError):
        existing = []

    existing_call_ids = {e.get("vapi_call_id") for e in existing if e.get("vapi_call_id")}
    added = []

    async with httpx.AsyncClient(timeout=30) as client:
        print("Fetching ALL calls from VAPI v2 API...")
        try:
            vapi_calls = await fetch_all_vapi_calls(client)
        except Exception as e:
            print(f"ERROR fetching VAPI calls: {e}")
            return
        print(f"\nTotal VAPI calls fetched: {len(vapi_calls)}\n")

        # Build GHL phone → contact lookup
        print("Loading GHL contacts for phone matching...")
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
                full = contact_data.get("contact", {})
                name = full.get("name", name)
                contact_id_to_info[contact_id]["name"] = name
                raw_phone = full.get("phone", "")
                if raw_phone:
                    norm = normalize_phone(raw_phone)
                    phone_to_contact[norm] = contact_id
                    phone_to_contact[norm[-10:]] = contact_id
            except Exception:
                pass
            if (i + 1) % 20 == 0:
                print(f"  Loaded {i+1}/{len(opps)} contacts...")
        print(f"Loaded {len(opps)} contacts, {len(phone_to_contact)} phone entries\n")

        # Process each VAPI call
        for call in vapi_calls:
            call_id = call.get("id", "")
            if not call_id or call_id in existing_call_ids:
                continue

            customer_phone = (call.get("customer") or {}).get("number", "")
            ended_reason = call.get("endedReason", "")
            started_at = call.get("startedAt", "") or call.get("createdAt", "")
            ended_at = call.get("endedAt", "")
            created_at = call.get("createdAt", "")

            # callStarted: NO if it errored before connecting
            call_started = "NO" if ended_reason.startswith("call.start.error") else "YES"

            # Duration
            duration_seconds = 0
            if call_started == "YES" and started_at and ended_at:
                from datetime import datetime as _dt
                try:
                    s = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                    e = _dt.fromisoformat(ended_at.replace("Z", "+00:00"))
                    duration_seconds = max(0, int((e - s).total_seconds()))
                except Exception:
                    pass

            # Property address from variableValues
            vv = (call.get("assistantOverrides") or {}).get("variableValues") or {}
            property_address = vv.get("property_address", "")
            contact_name_from_vapi = vv.get("name", "")

            # Structured outputs → summary + outcome
            so = (call.get("artifact") or {}).get("structuredOutputs") or {}
            summary = (so.get(SO_CALL_SUMMARY_ID) or {}).get("result", "")
            outcome = parse_outcome(so)

            # Match to GHL contact
            norm_phone = normalize_phone(customer_phone) if customer_phone else ""
            contact_id = phone_to_contact.get(norm_phone) or phone_to_contact.get(norm_phone[-10:] if len(norm_phone) >= 10 else "")
            info = contact_id_to_info.get(contact_id, {})
            name = info.get("name", "") or contact_name_from_vapi or customer_phone
            stage = info.get("stage", "Unknown")

            entry = {
                "vapi_call_id": call_id,
                "contact_id": contact_id or "",
                "name": name,
                "phone": customer_phone,
                "stage": stage,
                "property_address": property_address,
                "triggered_at": created_at,
                "started_at": started_at,
                "ended_at": ended_at,
                "call_started": call_started,
                "duration_seconds": duration_seconds,
                "ended_reason": ended_reason,
                "ai_summary": summary,
                "outcome": outcome,
                "backfilled": True,
            }
            added.append(entry)
            existing_call_ids.add(call_id)

            status = outcome or ended_reason
            started_label = f"✅ {duration_seconds}s" if call_started == "YES" else "❌ no connect"
            print(f"  {started_label} | {name} | {status[:60]}")

    if added:
        all_entries = existing + added
        with open(CALL_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_entries, f, ensure_ascii=False, indent=2)
        print(f"\nAdded {len(added)} new entries. Total: {len(all_entries)}")
    else:
        print("\nNo new entries found.")


if __name__ == "__main__":
    asyncio.run(main())
