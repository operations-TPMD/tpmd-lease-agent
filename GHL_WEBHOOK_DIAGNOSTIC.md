# GHL→Base44 Webhook Diagnostics

## Issue Summary
Incoming GHL messages are not being transferred to Base44. The workflow that's supposed to process Work Orders and Leasing Leads isn't working.

## Root Causes Identified

### 1. ✅ FIXED: Critical Bug in handleWorkOrderInbound (entry.ts:608)
**Status:** RESOLVED in commit 56eec2d

**Issue:** Undefined variable `dry` being used before declaration
- Line 608: `if (isPositive && !dry)` — but `dry` was only defined on line 653
- **Impact:** ReferenceError crash when webhook processes tenant post-repair feedback

**Fix Applied:** Moved `const dry = config.dry_run_mode as boolean;` to line 599 (before usage)

---

## Required Configuration Checks

### 2. ⚠️ BASE44_SERVICE_TOKEN — MUST BE SET
**Location:** ghlInboundWebhook function environment variables
**What it is:** A token that allows ghlInboundWebhook to invoke other Base44 functions

**How to Fix:**
1. Go to Base44 dashboard → Your App
2. Navigate to Settings → Environment Variables
3. Add or verify: `BASE44_SERVICE_TOKEN=<your-service-token>`
4. Get the token from: Base44 dashboard → API Tokens → Service Role

**Why it's needed:** 
- Without it, `base44.asServiceRole.functions.invoke()` calls fail (lines 280, 287)
- These calls invoke `handleWorkOrderInbound` and `handleLeasingLead`

---

### 3. ⚠️ Webhook Endpoint URL — MUST BE CONFIGURED IN GHL
**What it is:** The URL in GHL that points to the Base44 ghlInboundWebhook function

**How to Configure:**
1. In GHL, create/edit a Workflow
2. Trigger: "Message Received" or "Customer Reply"
3. Action: Send Webhook → POST
4. URL: `https://<your-base44-app>.base44.app/functions/ghlInboundWebhook`
5. Body (JSON):
   ```json
   {
     "contact_id": "{{contact.id}}",
     "phone": "{{contact.phone}}",
     "message": "{{message.body}}",
     "tags": "{{contact.tags}}",
     "customData": {}
   }
   ```

**Get your webhook URL:**
- Ask: What's your Base44 app's deployed URL?
- Format: The ghlInboundWebhook function is auto-deployed to your Base44 app

---

### 4. ⚠️ GHL API Credentials
**What's needed:** Environment variables in ghlInboundWebhook
- `GHL_API_KEY` — Your GHL API key
- `GHL_LOCATION_ID` — Your GHL location ID

**Where to set:** Base44 dashboard → Settings → Environment Variables

---

## Testing Checklist

- [ ] Verify `BASE44_SERVICE_TOKEN` is set in Base44
- [ ] Confirm ghlInboundWebhook function is deployed
- [ ] Verify GHL workflow has correct webhook URL configured
- [ ] Check GHL workflow is active and triggered on "Message Received"
- [ ] Send test SMS from GHL to verify webhook fires
- [ ] Check Base44 function logs for errors
- [ ] Confirm Work Orders or Leasing messages are being processed

---

## Monitoring

To confirm the fix is working:
1. Send an inbound message via GHL
2. Check Base44 function logs (Dashboard → Functions → ghlInboundWebhook)
3. Look for successful invocation of `handleWorkOrderInbound` or `handleLeasingLead`
4. Verify entity creation/updates in Base44

---

## Next Steps

1. **Immediate:** Verify `BASE44_SERVICE_TOKEN` is set
2. **Follow-up:** Test webhook endpoint from GHL
3. **Monitor:** Watch Base44 logs for 24-48 hours to confirm stability

