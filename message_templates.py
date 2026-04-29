"""
Message templates for the Lease Agent.
Each template is a function that takes lead data and returns a formatted message.
Templates are organized by stage/situation.
"""

# ── Template definitions ─────────────────────────────────────────────────────

TEMPLATES = {
    # ── New Lead / Verification Auto-Sent (promotional, property-focused) ────
    "welcome_initial": {
        "name": "Welcome + Property Highlight",
        "stage": ["New Lead", "Verification Auto-Sent"],
        "category": "promotional",
        "template": (
            "Hi {name}! {headline} "
            "{address} — {beds_baths}, ${rent}/mo. {special_offer} "
            "To schedule a tour, verify your ID here: {id_link} (takes 30 seconds!)"
        ),
    },
    "welcome_reminder_24h": {
        "name": "24h Reminder — Focus on features",
        "stage": ["New Lead", "Verification Auto-Sent"],
        "category": "promotional",
        "template": (
            "Hey {name}! Quick reminder about {address}: "
            "{beds_baths}, fresh renovations, move-in ready! ${rent}/mo. "
            "{special_offer} Verify your ID to lock in a showing: {id_link}"
        ),
    },
    "welcome_reminder_48h": {
        "name": "48h Reminder — Urgency + unique features",
        "stage": ["New Lead", "Verification Auto-Sent"],
        "category": "promotional",
        "template": (
            "{name}, {address} is getting a LOT of interest! "
            "{property_highlights} Only ${rent}/mo. "
            "{special_offer} Don't miss out — verify your ID now: {id_link}"
        ),
    },
    "welcome_final_push": {
        "name": "Final push — scarcity",
        "stage": ["New Lead", "Verification Auto-Sent"],
        "category": "promotional",
        "template": (
            "Last call {name}! {address} ({beds_baths}, ${rent}/mo) "
            "has multiple showings booked this week. "
            "{special_offer} Secure your spot: {id_link} — Sivan, TPMD"
        ),
    },

    # ── ID Verified (push to book showing) ───────────────────────────────────
    "book_showing": {
        "name": "ID Verified — Book showing",
        "stage": ["ID Verified"],
        "category": "conversion",
        "template": (
            "Great news {name}! Your ID is verified. "
            "Ready to see {address}? {beds_baths}, ${rent}/mo — {special_offer} "
            "Reply with a date/time that works or call us at 888-850-2737!"
        ),
    },
    "book_showing_followup": {
        "name": "Showing nudge — property details",
        "stage": ["ID Verified"],
        "category": "conversion",
        "template": (
            "{name}, your verified ID is ready to go! {address} won't last long — "
            "{property_highlights} "
            "When would you like to tour? Call/text us anytime!"
        ),
    },

    # ── Showing Scheduled (reminders) ────────────────────────────────────────
    "showing_reminder_day_before": {
        "name": "Day-before reminder + code",
        "stage": ["Showing Scheduled"],
        "category": "reminder",
        "template": (
            "Reminder: Your tour of {address} is tomorrow! "
            "Access code: {code}. "
            "Let us know if you need to reschedule. See you there! — TPMD"
        ),
    },
    "showing_reminder_day_of": {
        "name": "Day-of reminder + directions",
        "stage": ["Showing Scheduled"],
        "category": "reminder",
        "template": (
            "Today's the day! Your tour of {address} is coming up. "
            "Access code: {code}. "
            "Text us when you arrive or if you need help finding it!"
        ),
    },
    "showing_no_show": {
        "name": "No-show re-engagement",
        "stage": ["Showing Scheduled"],
        "category": "re-engagement",
        "template": (
            "Hi {name}, looks like we missed you at {address}! "
            "No worries — the unit is still available at ${rent}/mo. "
            "{special_offer} Want to reschedule? Just reply with a good time."
        ),
    },

    # ── Post-Showing / Tenant Feedback ───────────────────────────────────────
    "post_showing_feedback": {
        "name": "Post-tour feedback request",
        "stage": ["Showing Scheduled", "Tenant Feedback"],
        "category": "feedback",
        "template": (
            "Hi {name}! How was your tour of {address}? "
            "Would love to hear your thoughts! "
            "If you're ready to move forward, I can send you the application right away."
        ),
    },
    "post_showing_push_app": {
        "name": "Push application",
        "stage": ["Tenant Feedback"],
        "category": "conversion",
        "template": (
            "{name}, glad you liked {address}! "
            "Here's your application link: {app_link} "
            "{special_offer} Spots are filling up — apply today! — Sivan"
        ),
    },

    # ── Application Sent ─────────────────────────────────────────────────────
    "app_followup": {
        "name": "Application follow-up",
        "stage": ["Application Sent"],
        "category": "conversion",
        "template": (
            "Hi {name}, just checking in — did you get a chance to start your "
            "application for {address}? Let me know if you have any questions! — Sivan"
        ),
    },

    # ── Cold Lead / Re-engagement ────────────────────────────────────────────
    "cold_reengage": {
        "name": "Cold lead re-engagement",
        "stage": ["*"],
        "category": "re-engagement",
        "template": (
            "Hi {name}! Still thinking about {address}? "
            "{beds_baths}, ${rent}/mo — {special_offer} "
            "Let me know if you'd like to schedule a tour! — Sivan, TPMD"
        ),
    },
    "final_followup": {
        "name": "Final follow-up before marking lost",
        "stage": ["*"],
        "category": "re-engagement",
        "template": (
            "{name}, this is my last check-in about {address}. "
            "If you're still interested, I'm here to help! "
            "Otherwise, wishing you the best in your search. — Sivan, TPMD"
        ),
    },

    # ── Disqualified ─────────────────────────────────────────────────────────
    "disqualified_polite": {
        "name": "Polite disqualification",
        "stage": ["Lost"],
        "category": "closing",
        "template": (
            "Hi {name}, unfortunately {address} isn't the right fit "
            "based on the property requirements. "
            "We wish you the best in your search! — TPMD"
        ),
    },

    # ── Voice Bot Trigger ────────────────────────────────────────────────────
    "voice_bot_intro": {
        "name": "Pre-call SMS before voice bot",
        "stage": ["ID Verified", "Call: No Answer"],
        "category": "voice",
        "template": (
            "Hi {name}! We'll be giving you a quick call shortly about "
            "{address} to help schedule your tour. Talk soon! — TPMD"
        ),
    },
}


def format_template(template_id: str, lead: dict) -> str | None:
    """Format a message template with lead data."""
    tmpl = TEMPLATES.get(template_id)
    if not tmpl:
        return None

    # Build property highlights from summary (first 2-3 interesting points)
    summary = lead.get("property_summary", "")
    highlights = ""
    if summary:
        lines = [l.strip() for l in summary.split("\n") if l.strip() and ":" not in l[:15]]
        highlights = ". ".join(lines[:2]) if lines else ""

    # Extract beds/baths from summary
    beds_baths = ""
    if summary:
        for line in summary.split("\n"):
            if "bd" in line.lower() or "bed" in line.lower():
                beds_baths = line.strip().split("|")[0].strip()
                break

    # Extract rent
    rent = ""
    if summary:
        for line in summary.split("\n"):
            if "rent" in line.lower() and "$" in line:
                rent = line.split("$")[-1].split("\n")[0].strip().replace(",", "")
                break

    # Clean special offer
    special = lead.get("special_offer", "")
    if special:
        special = special.replace("***", "").strip()
        if not special.endswith("!"):
            special += "!"

    values = {
        "name": lead.get("name", "").split()[0] if lead.get("name") else "there",
        "address": lead.get("property_address", "the property"),
        "headline": lead.get("property_headline", "")[:100],
        "beds_baths": beds_baths or "details available",
        "rent": rent or "call for pricing",
        "special_offer": special or "",
        "property_highlights": highlights or "",
        "id_link": f"https://thepropertymanagementdoctor.com/go?c={lead.get('contact_id', '')}&t=id",
        "code": lead.get("lock_code", "N/A"),
        "app_link": lead.get("application_url", ""),
    }

    try:
        return tmpl["template"].format(**values).strip()
    except (KeyError, ValueError):
        return tmpl["template"]


def get_templates_for_stage(stage: str) -> list[dict]:
    """Get applicable templates for a given pipeline stage."""
    result = []
    for tid, tmpl in TEMPLATES.items():
        if stage in tmpl["stage"] or "*" in tmpl["stage"]:
            result.append({"id": tid, **tmpl})
    return result
