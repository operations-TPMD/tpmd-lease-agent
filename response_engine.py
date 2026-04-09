"""
Response Time Engine for Lease Agent

Two modes:
1. PERIODIC — Runs every 2 hours (9am, 11am, 1pm, 3pm, 5pm, 7pm ET), scans active leads, takes action
2. INSTANT — Webhook endpoint that GHL calls when a lead sends a message/takes action

Setup for instant responses:
  In GHL, create a workflow triggered by "Customer Reply" or "Inbound Message"
  that sends a webhook to: http://YOUR_SERVER:8000/api/webhook/inbound
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from lease_agent import (
    get_all_opportunities, enrich_lead, ask_claude,
    send_sms, update_stage, STAGE_MAP, STAGE_NAME_TO_ID,
    GHL_API_KEY, GHL_LOCATION_ID, OPENAI_API_KEY,
    ghl_headers, GHL_API_BASE,
)

logger = logging.getLogger("response_engine")

# Business hours: 9am - 7pm Eastern (UTC-4)
BIZ_START_HOUR = 13  # 9am ET = 13 UTC
BIZ_END_HOUR = 23    # 7pm ET = 23 UTC


def is_business_hours() -> bool:
    """Check if current time is within business hours."""
    now = datetime.now(timezone.utc)
    return BIZ_START_HOUR <= now.hour < BIZ_END_HOUR


async def periodic_scan(dry_run: bool = False) -> dict:
    """Scan all active leads and take appropriate action.
    Returns summary of actions taken."""
    if not is_business_hours():
        return {"status": "outside_business_hours", "actions": 0}

    summary = {"status": "completed", "actions": 0, "skipped": 0, "errors": 0, "details": []}
    RATE_LIMIT_DELAY = 0.3  # 300ms delay between AI calls to avoid rate limiting

    async with httpx.AsyncClient(timeout=30) as client:
        opps = await get_all_opportunities(client)
        logger.info(f"Periodic scan: {len(opps)} opportunities found")

        for opp in opps:
            stage_id = opp.get("pipelineStageId", "")
            stage_name = STAGE_MAP.get(stage_id, stage_id)
            opp_status = opp.get("status", "")

            # Skip terminal stages
            if stage_name in ("Leased / Won", "Lost") or opp_status == "lost":
                summary["skipped"] += 1
                continue

            try:
                lead = await enrich_lead(client, opp)

                if lead["dnd"]:
                    summary["skipped"] += 1
                    continue

                # Add delay to avoid rate limiting
                await asyncio.sleep(RATE_LIMIT_DELAY)
                decision = await ask_claude(client, lead)
                action = decision.get("action", "skip")

                if action == "skip":
                    summary["skipped"] += 1
                    continue

                detail = {
                    "name": lead["name"],
                    "stage": stage_name,
                    "action": action,
                    "message": decision.get("message", ""),
                    "new_stage": decision.get("new_stage", ""),
                    "reasoning": decision.get("reasoning", ""),
                }

                if not dry_run:
                    if action in ("send_sms", "send_sms_and_update_stage"):
                        if decision.get("message"):
                            await send_sms(client, lead["contact_id"], decision["message"])
                            detail["sms_sent"] = True

                    if action in ("update_stage", "send_sms_and_update_stage"):
                        if decision.get("new_stage"):
                            await update_stage(client, opp["id"], decision["new_stage"])
                            detail["stage_updated"] = True

                summary["actions"] += 1
                summary["details"].append(detail)

            except Exception as e:
                summary["errors"] += 1
                logger.error(f"Error processing {opp.get('contactId', '?')}: {e}")

    logger.info(f"Periodic scan complete: {summary['actions']} actions, {summary['skipped']} skipped, {summary['errors']} errors")
    return summary


async def handle_inbound(contact_id: str, message_body: str = "", dry_run: bool = False) -> dict:
    """Handle an inbound message/action from a lead — instant response.
    Called by webhook when GHL detects inbound activity."""

    if not is_business_hours():
        return {"status": "queued_outside_hours", "contact_id": contact_id}

    async with httpx.AsyncClient(timeout=30) as client:
        # Find the contact's opportunity in the lease pipeline
        resp = await client.get(
            f"{GHL_API_BASE}/contacts/{contact_id}",
            headers=ghl_headers(),
        )
        if resp.status_code != 200:
            return {"status": "error", "message": "Contact not found"}

        # Search for their opportunity
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

        if opps_resp.status_code != 200:
            return {"status": "error", "message": "Opportunity search failed"}

        opps = opps_resp.json().get("opportunities", [])
        if not opps:
            return {"status": "no_opportunity", "contact_id": contact_id}

        opp = opps[0]
        stage_name = STAGE_MAP.get(opp.get("pipelineStageId", ""), "Unknown")

        # Skip terminal stages
        if stage_name in ("Leased / Won", "Lost") or opp.get("status") == "lost":
            return {"status": "skipped", "reason": f"Lead in {stage_name}"}

        try:
            lead = await enrich_lead(client, opp)

            if lead["dnd"]:
                return {"status": "skipped", "reason": "DND enabled"}

            decision = await ask_claude(client, lead)
            action = decision.get("action", "skip")

            result = {
                "status": "processed",
                "contact_id": contact_id,
                "name": lead["name"],
                "stage": stage_name,
                "action": action,
                "message": decision.get("message", ""),
                "new_stage": decision.get("new_stage", ""),
                "reasoning": decision.get("reasoning", ""),
            }

            if not dry_run and action != "skip":
                if action in ("send_sms", "send_sms_and_update_stage"):
                    if decision.get("message"):
                        await send_sms(client, contact_id, decision["message"])
                        result["sms_sent"] = True

                if action in ("update_stage", "send_sms_and_update_stage"):
                    if decision.get("new_stage"):
                        await update_stage(client, opp["id"], decision["new_stage"])
                        result["stage_updated"] = True

            return result

        except Exception as e:
            return {"status": "error", "message": str(e)[:200]}


class PeriodicScheduler:
    """Runs periodic_scan every 2 hours: 9am, 11am, 1pm, 3pm, 5pm, 7pm ET."""

    # Run times in UTC (ET is UTC-4, so add 4 hours)
    RUN_HOURS_UTC = [13, 15, 17, 19, 21, 23]  # 9am, 11am, 1pm, 3pm, 5pm, 7pm ET

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.running = False
        self._task = None
        self.last_run = None
        self.last_result = None
        self._last_run_hour = None  # Track last run hour to avoid duplicate runs

    async def _loop(self):
        self.running = True
        while self.running:
            now = datetime.now(timezone.utc)
            current_hour = now.hour

            # Check if it's time to run
            should_run = False
            if is_business_hours():
                if current_hour in self.RUN_HOURS_UTC and self._last_run_hour != current_hour:
                    should_run = True

            if should_run:
                logger.info(f"Periodic scan starting at {now.isoformat()} (dry_run={self.dry_run})")
                try:
                    self.last_result = await periodic_scan(dry_run=self.dry_run)
                    self.last_run = now.isoformat()
                    self._last_run_hour = current_hour
                except Exception as e:
                    logger.error(f"Periodic scan failed: {e}")
                    self.last_result = {"status": "error", "message": str(e)}

            await asyncio.sleep(60)  # Check every minute

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info(f"Scheduler started: every 2 hours (9am, 11am, 1pm, 3pm, 5pm, 7pm ET), dry_run={self.dry_run}")

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def set_live(self):
        """Switch from dry-run to live mode."""
        self.dry_run = False
        logger.info("Scheduler switched to LIVE mode")

    def set_dry_run(self):
        """Switch to dry-run mode."""
        self.dry_run = True
        logger.info("Scheduler switched to DRY RUN mode")
