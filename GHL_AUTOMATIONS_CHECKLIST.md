# GHL Automations Checklist — The Property Management Doctor

Complete all 5 workflows below for the lease agent system to work end-to-end.

---

## 1. ✅ INSTANT MESSAGE RESPONSE (Webhook)

**Trigger:** Customer sends inbound SMS/message

**Setup:**
- In GHL: Create new Workflow
- Trigger: "Message Received" or "Customer Reply"
- Contact pipeline: "Lease"

**Action:**
- Send Webhook: `POST` to `https://YOUR_DOMAIN:8000/api/webhook/inbound`
- Body (JSON):
  ```json
  {
    "contact_id": "{{contact.id}}",
    "message": "{{message.body}}"
  }
  ```

**Result:** AI instantly responds with SMS/stage change (if in LIVE mode)

---

## 2. ✅ VOICE BOT CALL TRIGGER

**Trigger:** Contact tag added

**Setup:**
- In GHL: Create new Workflow
- Trigger: "Contact Tag Added"
- Tag name: `call_for_showing`
- Contact pipeline: "Lease"

**Action:**
- Call Voice Bot (AI Agent)
- Agent ID: `69d658fa4ccb41abc9c6f543`
- Wait for call to complete

**After Call Actions:**
- Check: "Add call summary as a note to the contact" ✅
- Check: "Trigger Workflow when call is completed" ✅

**Optional - Send SMS after call:**
- Action: Send SMS
- Message: "Thanks for scheduling your showing at {{contact.property_address}}! We'll text you the details soon."

---

## 3. ✅ VOICE BOT CALL SUCCESS (after call completes)

**Trigger:** Voice call completed (from Workflow #2)

**Setup:**
- In GHL: Create new Workflow
- Trigger: "Workflow Completed" (when Workflow #2 finishes)
- Condition: Call was successful (check call notes for confirmation)

**Actions:**
- Move Contact to Stage: "Showing Scheduled"
- Add Note: "Voice bot scheduled showing - check call summary for time"

---

## 4. ✅ VOICE BOT CALL FAILED (fallback SMS)

**Trigger:** Voice call failed/no answer (from Workflow #2)

**Setup:**
- In GHL: Create new Workflow
- Trigger: "Workflow Completed" (when Workflow #2 finishes)
- Condition: Call was NOT successful / no answer

**Actions:**
- Send SMS to contact:
  ```
  "Hi {{contact.first_name}}, we tried calling to schedule your showing at {{contact.property_address}}. When works best for you? Reply with your preferred date and time."
  ```

---

## 5. ✅ SHOWING REMINDERS & NO-SHOW ALERTS

**For Reminders (optional, can be manual or automated):**

**Trigger:** Contact in "Showing Scheduled" stage with showing_date set
- 15 minutes before showing time:
  - Send SMS: "Reminder: Your showing at {{contact.property_address}} is in 15 minutes!"
  
- At showing time:
  - Send SMS: "Are you here? Reply YES when you arrive at {{contact.property_address}}"

**For No-Show Alert:**
- 30 minutes after showing time with no response:
  - Send SMS to team number: "[URGENT] {{contact.first_name}} no-show at {{contact.property_address}} (scheduled {{custom_field.showing_date}})"
  - Move Contact to Stage: "Lost"
  - Add tag: `no_show`

---

## Summary

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| 1 | Customer messages | Instant AI response |
| 2 | Tag: `call_for_showing` | Voice bot calls to book |
| 3 | Call succeeded | Confirm stage change |
| 4 | Call failed | SMS fallback |
| 5 | Showing scheduled | Reminders + no-show alerts |

---

## Important Notes

- **Webhook URL:** Replace `YOUR_DOMAIN` with your deployed server (e.g., `tpmd-lease-agent.railway.app`)
- **Voice Bot Agent ID:** `69d658fa4ccb41abc9c6f543` (already set)
- **Custom Fields to track:**
  - `showing_date` - when customer scheduled the showing
  - `property_address` - pulled from "properties" section in GHL
  - `last_call_status` - tracks voice bot call result
- **Tags to use:**
  - `call_for_showing` - triggers voice bot
  - `no_show` - marks leads who didn't show up
- **Business Hours:** 9am-7pm ET (our system respects this, GHL workflows run anytime)

---

## Testing Checklist

- [ ] Webhook #1: Send test SMS, confirm AI responds
- [ ] Workflow #2: Manually add tag to test contact, confirm voice bot calls
- [ ] Workflow #3: Verify "Showing Scheduled" stage update after successful call
- [ ] Workflow #4: Verify SMS sent if call fails
- [ ] Workflow #5: Test reminders (may be manual first time)
- [ ] Switch dashboard to "LIVE" mode after testing

