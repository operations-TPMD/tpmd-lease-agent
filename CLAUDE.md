
# TPMD — The Property Management Doctor
## Claude Code Session Context

---

## Language Rules
- Respond in **Hebrew** for all explanations and conversation.
- All code, variable names, function names, error messages, and technical terms stay in **English**.
- Never mix Hebrew and English within the same sentence.
- Use separate paragraphs for Hebrew explanations and English-only code blocks for code.

---

## Testing Rules
- After every code change, run the relevant test command before presenting results.
- Do not present code as final until it runs without errors.
- If a test fails, fix it before returning — do not ask me to test it myself.

---

## Project Overview
**Company:** The Property Management Doctor (TPMD) — tpmd.io  
**Location:** Davie, Florida  
**Team:** Edan (founder), Sivan (co-owner), Victoria (Tenant Relations), coordinator, maintenance, VAs (Central America)  
**Repo:** `operations-TPMD/the-property-management-doctor`

---

## Tech Stack

| Tool | Role |
|------|------|
| **AppFolio** | Property management platform — source of truth for tenant, lease, and financial data |
| **GoHighLevel (GHL)** | CRM, phone system, automations, internal inbox, AI bot triggers |
| **Base44** | Internal ops dashboard and UI — all custom tools live here |
| **Make.com** | Automation middleware between GHL, Base44, AppFolio |
| **Vapi** | AI voice calls (inbound/outbound) |
| **TTLock** | Smart lock system for self-showing |
| **Deno / Edge Functions** | Backend logic running on Base44 |
| **GitHub** | This repo — all code synced here via Base44 GitHub integration |

---

## Core Architecture — Leasing Pipeline (Most Important)

This is the main system we work on. Full self-showing flow:

### Inbound Lead Flow
1. Lead comes in via GHL (web form, call, or text)
2. Make.com webhook triggers Base44 router function
3. Router identifies: leasing inquiry vs. work order vs. other
4. Leasing handler checks property availability in AppFolio
5. If available → Didit ID verification link sent to prospect
6. After ID verified → TTLock door code generated and sent
7. Prospect does self-showing
8. Post-tour: Vapi outbound call for follow-up
9. If interested → AppFolio application link sent

### Inbound Call Routing
- Each property has its own GHL phone number
- Identified via `{{phoneCall.to}}` in GHL webhook
- Vapi AI bot handles leasing questions
- Guardrail: bot must NOT offer to schedule mid-tour

### Key Functions (Base44 / Deno)
- `router` — main webhook dispatcher
- `leasingHandler` — handles leasing inquiries
- `workOrderHandler` — handles maintenance requests
- `scanAllLeads` — periodic AI agent that reviews GHL opportunities and decides on outreach

---

## AppFolio Data Pipeline

AppFolio does not have a real API — data is pulled via **CSV exports** from Report Builder.

### Investor Reporting (V9 — current version)
Two clean Report Builder exports:
1. **Main report** — combines: Rent Roll + General Ledger (32-account whitelist) + Occupancy Summary + Property Directory
2. **Work Orders report** — separate export

Rules:
- Use relative date filters (not hardcoded dates)
- Never overwrite existing owner email data
- Rodny does QA before investor distribution

---

## Base44 Conventions
- Claude writes all significant code — Base44 AI is only for simple UI tasks
- Always provide **complete files**, never partial diffs
- Functions are modular — one responsibility per function
- Gmail API used directly (not SendEmail integration, to avoid credit drain)

---

## GHL Conventions
- Automations trigger via webhooks to Base44
- Internal team communication via GHL Internal Inbox
- Phone numbers are per-property, identified by `{{phoneCall.to}}`

---

## Other Active Systems
- **VA Recruitment** — Base44 app with toggle, task filtering, application form, video upload, bulk archive
- **Pricing Page** — modular service store (Maintenance / Leasing / Tenant Relations / Full Management) with combo detection and savings calculator
- **Past Due / Lease Expiration / Move In / Move Out** — automated pipelines in progress, pulling from AppFolio CSV exports, surfaced in Base44 UI

---

## End of Session Protocol
At the end of every session, before we finish:
1. Summarize what we built or changed in this session
2. Ask me: "Should I update CLAUDE.md with anything from this session?"
3. If yes — update the relevant sections automatically, then commit the file to GitHub

---

## What NOT to Do
- Do not use `SendEmail` Base44 integration (burns credits) — use Gmail API directly
- Do not hardcode dates in AppFolio report filters
- Do not present partial code — always return complete files
- Do not offer mid-tour scheduling in the Vapi leasing bot
