
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

### Two Report Pipelines

| Pipeline | Function | UI Component | Purpose |
|----------|----------|--------------|---------|
| **V8** | `buildReportsFromBulk` | `ReportGenerator` (V8 label) | Monthly Portfolio Report — team/management use. Kept as backup while V9 is validated. |
| **V9** | `buildInvestorReportsV9` | `ReportGeneratorV9` (V9 label) | Investor Report — sent to owners monthly. **Current active pipeline.** |

### V9 Input Files (3 CSVs)
1. **General Ledger** (`general_ledger-*.csv`) — all transactions for the month, flat row-per-line format. Source of all income, expense, distribution data.
2. **Rent Roll Itemized** (`report_builder-rent_roll_itemized-*.csv`) — tenant occupancy, lease dates, owner names.
3. **Trust Account Balance** (`trust_account_balance-*.csv`) — reserve/cash balance per property.

### GL Classification Logic (direction-based, verified against AppFolio Owner Statements May–June 2026)
- **Income** (4xxx): `credit - debit`. Direction matters — same account can be income or refund.
- **Distribution** (3250): `abs(debit - credit)`.
- **Expense** (5xxx–7xxx + 2120 Clearing): `debit - credit`.
- **6113 Vendor Discounts**: tagged `expense_markup`, merged with vendor cost row on same Reference+Date. Only merge when EXACTLY 1 cost + 1 markup — otherwise warn + emit separately.
- **Skipped accounts**: 4440 (Application Fee — doesn't net cleanly across periods), 6002, 4210, 4200, 1150, 1160, 2101.
- **Expense categories**: 5xxx = maintenance; 6121/6122/6123/6130 = Mortgage & Interest; 6161/6162 = Taxes; 6100–6119 = Management & Fees; 6120–6149 = maintenance; 6150–6179 = Utilities; 6180–6199 = Insurance/Legal/Other.
- **Date filter**: uses each row's own Date column. 1150 rows are skipped entirely so their dates never touch income totals.

### Gmail Auto-fetch (V9)
V9 can fetch CSVs from AppFolio scheduled report emails automatically:
- GL: `from:donotreply@appfolio.com subject:"General Ledger"`, prefix `general_ledger-`
- Rent Roll: `from:donotreply@appfolio.com subject:"Rent Roll"`, prefix `report_builder-`
- Trust Balance: `from:donotreply@appfolio.com subject:"Trust Account Balance"`, prefix `trust_account_balance-`
- **Note**: Trust Balance filename prefix needs verification after first scheduled email is received.

### Rules
- Use relative date filters (not hardcoded dates)
- Never overwrite existing owner email data
- Rodny does QA before investor distribution
- V8 legacy functions (`parseGLIncome`, `parseCashFlowDetail`, etc.) kept with `// LEGACY` comments until V9 GL output is fully verified month-by-month

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
