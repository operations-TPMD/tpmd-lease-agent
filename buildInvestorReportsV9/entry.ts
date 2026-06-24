// =====================================================================
// Monthly Investor Report Builder - V9 (Clean Rewrite)
// =====================================================================
// NEW file — V8 (buildReportsFromBulk) is UNTOUCHED and keeps running.
//
// WHY V9:
//   V8 reconstructed financials from 7 fragile AppFolio CSV exports
//   (CF Comparison, Income Statement, Cash Flow Detail, Rent Roll, Work
//   Order, Property Directory, Owner Directory). Most were matrix-style
//   reports requiring header detection and reconciliation hacks.
//
//   V9 consumes exactly 2 simple row-per-event exports:
//     GL_REPORT  — AppFolio Report Builder: Itemized Rent Roll + GL
//                  (32-account whitelist) + Occupancy Summary +
//                  Property Directory, combined in one report.
//                  "Show Reversed Transactions" must be OFF in AppFolio
//                  so reversal netting happens at the source.
//     WO_REPORT  — Plain AppFolio Work Order report, Completed only,
//                  kept separate to avoid cross-join inflation.
//
//   Both reports are auto-fetched from Gmail each month. Manual
//   override is available by passing payload.glReport and/or
//   payload.workOrderReport as CSV strings.
//
// FINANCIAL ARCHITECTURE:
//   All income / expense / NOI math comes ONLY from GL_REPORT.
//   WO_REPORT is used ONLY to render the descriptive "Work Orders"
//   list. It is NEVER summed into financials.
//
// OWNER EMAIL:
//   Primary source: OWNER_EMAIL_MAP hardcoded below (fill in actual
//   addresses — emails change rarely so this is safe to maintain here).
//   Fallback: existing DirectoryProperty entity records from V8's prior
//   runs, so V9 works immediately even before the map is populated.
//   Never overwrites a non-blank owner_email with blank.
//
// RETURN: { reports: [{ ownerName, ownerEmail, pdfUrl, htmlContent,
//           warnings }], success: true }
// AUTH:   admin or owner role required.
// =====================================================================

import { createClientFromRequest } from 'npm:@base44/sdk@0.8.30';

// ─────────────────────────────────────────────────────────────────────
// Generic utilities (identical contract to V8 — never change these)
// ─────────────────────────────────────────────────────────────────────

function parseCsvSafely(content) {
  if (!content || typeof content !== 'string' || content.trim().length === 0) return [];
  content = content.replace(/^﻿/, '');
  try {
    const rows = [];
    let row = [], cur = '', inQuote = false;
    if (!content.includes('"')) {
      for (const line of content.split(/\r?\n/)) {
        if (!line.trim()) continue;
        const r = line.split(',');
        if (r.some(c => c.trim())) rows.push(r);
      }
      return rows;
    }
    let i = 0;
    const len = content.length;
    while (i < len) {
      if (inQuote) {
        const q = content.indexOf('"', i);
        if (q === -1) { cur += content.substring(i); break; }
        cur += content.substring(i, q);
        i = q;
        if (content[i + 1] === '"') { cur += '"'; i += 2; }
        else { inQuote = false; i++; }
      } else {
        let nc = content.indexOf(',', i);
        let nq = content.indexOf('"', i);
        let nl = content.indexOf('\n', i);
        if (nc === -1) nc = len;
        if (nq === -1) nq = len;
        if (nl === -1) nl = len;
        const ns = Math.min(nc, nq, nl);
        cur += content.substring(i, ns);
        i = ns;
        if (i === len) break;
        if (i === nq) { inQuote = true; i++; }
        else if (i === nc) { row.push(cur); cur = ''; i++; }
        else if (i === nl) {
          if (cur.endsWith('\r')) cur = cur.slice(0, -1);
          row.push(cur);
          if (row.some(c => c.trim())) rows.push(row);
          row = []; cur = ''; i++;
          if (content[i] === '\r') i++;
        }
      }
    }
    if (cur !== '' || row.length > 0) { row.push(cur); if (row.some(c => c.trim())) rows.push(row); }
    return rows;
  } catch { return []; }
}

function parseMoney(str) {
  if (str === null || str === undefined) return 0;
  const s = String(str).trim();
  if (!s) return 0;
  const isNeg = s.includes('(') || s.startsWith('-');
  const num = parseFloat(s.replace(/[^0-9.]/g, '')) || 0;
  return isNeg ? -num : num;
}

function propertiesMatch(a, b) {
  if (!a || !b) return false;
  const norm = s => s.toString().toLowerCase().replace(/\(.*?\)/g, '').replace(/[^a-z0-9]/g, '').trim();
  const na = norm(a), nb = norm(b);
  if (!na || !nb) return false;
  if (na.length < 8 || nb.length < 8) return na === nb;
  return na.includes(nb) || nb.includes(na);
}

function cleanPropertyName(raw) {
  return String(raw || '').split(' - ')[0].trim();
}

function isExcludedProperty(name) {
  const l = String(name || '').toLowerCase();
  return l.includes('test property') || l.includes('pm doctor admin') ||
         l.includes('pm doctor-') || l.includes('pm doctor operation') ||
         l.includes('admin property') || l.includes('operation account');
}

function findHeaderRow(csvRows, requiredCols) {
  for (let i = 0; i < Math.min(5, csvRows.length); i++) {
    const rl = csvRows[i].map(c => (c || '').toString().toLowerCase().trim());
    if (requiredCols.every(r => rl.some(c => c === r || c.includes(r)))) return i;
  }
  return -1;
}

function colIndex(headerRow, name) {
  const lower = headerRow.map(h => (h || '').toString().toLowerCase().trim());
  let idx = lower.indexOf(name.toLowerCase());
  if (idx !== -1) return idx;
  return lower.findIndex(h => h.includes(name.toLowerCase()));
}

// ─────────────────────────────────────────────────────────────────────
// GL account classification — 32-account whitelist
// ─────────────────────────────────────────────────────────────────────

const INCOME_ACCOUNTS = new Set(['4100', '4101', '41022', '4210']);
const CONCESSION_ACCOUNT = '4210';
const DISTRIBUTION_ACCOUNTS = new Set(['3250']);

const MAINTENANCE_ACCOUNTS = new Set([
  '6141', '6142', '6143', '6144', '6145', '6146', '6147',
  '5004', '5007', '5008', '5009', '5010',
  '5000', '5005'
]);

const OPERATING_SUBCATEGORY = {
  '6111': 'Management & Fees', '6112': 'Management & Fees', '7896': 'Management & Fees',
  '6171': 'Utilities', '6172': 'Utilities', '6173': 'Utilities',
  '6174': 'Utilities', '6175': 'Utilities',
  '6091': 'Insurance, Legal & Other', '6092': 'Insurance, Legal & Other',
  '6093': 'Insurance, Legal & Other', '6161': 'Insurance, Legal & Other',
  '6150': 'Other Operating'
};

function classifyByNameFallback(accountName) {
  const l = String(accountName || '').toLowerCase();
  const maintKw = ['repair', 'plumb', 'septic', 'well', 'pest', 'rodent', 'termite',
    'inspection', 'maintenance', 'paint', 'roof', 'hvac', 'renovation', 'contractor'];
  if (maintKw.some(k => l.includes(k))) return { category: 'maintenance', subcategory: null };
  if (/management|commission|leasing fee/.test(l)) return { category: 'operating', subcategory: 'Management & Fees' };
  if (/water|electric|gas|garbage|sewer|utility|utilities/.test(l)) return { category: 'operating', subcategory: 'Utilities' };
  if (/insurance|tax|legal/.test(l)) return { category: 'operating', subcategory: 'Insurance, Legal & Other' };
  return { category: 'operating', subcategory: 'Other Operating' };
}

function classifyGLAccount(glAccountCell) {
  const raw = String(glAccountCell || '').trim();
  const numMatch = raw.match(/^(\d+)/);
  const num = numMatch ? numMatch[1] : '';
  const name = raw.includes(' - ') ? raw.split(' - ').slice(1).join(' - ').trim() : raw;
  if (INCOME_ACCOUNTS.has(num)) return { type: 'income', isConcession: num === CONCESSION_ACCOUNT, name };
  if (DISTRIBUTION_ACCOUNTS.has(num)) return { type: 'distribution', name };
  if (MAINTENANCE_ACCOUNTS.has(num)) return { type: 'expense', category: 'maintenance', subcategory: null, name };
  if (OPERATING_SUBCATEGORY[num]) return { type: 'expense', category: 'operating', subcategory: OPERATING_SUBCATEGORY[num], name };
  const fallback = classifyByNameFallback(name);
  return { type: 'expense', category: fallback.category, subcategory: fallback.subcategory, name, unrecognized: true };
}

// ─────────────────────────────────────────────────────────────────────
// Gmail ingestion — auto-fetch both report CSVs from inbox
// ─────────────────────────────────────────────────────────────────────

async function searchGmailForReport(authHeader, searchQuery, filenamePrefix, warnings, label) {
  try {
    const params = new URLSearchParams({ q: searchQuery, maxResults: '20' });
    const listRes = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/messages?${params}`,
      { headers: authHeader }
    );
    if (!listRes.ok) {
      warnings.push(`Gmail search for ${label} failed: HTTP ${listRes.status}`);
      return null;
    }
    const listData = await listRes.json();
    const messages = listData.messages || [];
    if (!messages.length) {
      warnings.push(`No Gmail message found for ${label} (query: ${searchQuery})`);
      return null;
    }

    // Messages are returned newest-first by default. Try each until we find
    // one with a matching attachment, so duplicates/resends are handled.
    for (const msg of messages) {
      const msgRes = await fetch(
        `https://gmail.googleapis.com/gmail/v1/users/me/messages/${msg.id}?format=full`,
        { headers: authHeader }
      );
      if (!msgRes.ok) continue;
      const msgData = await msgRes.json();

      // Walk parts looking for a CSV attachment with the right filename prefix
      const findAttachment = (parts) => {
        for (const part of (parts || [])) {
          const fname = part.filename || '';
          if (fname.startsWith(filenamePrefix) && fname.endsWith('.csv')) {
            return part;
          }
          if (part.parts) {
            const inner = findAttachment(part.parts);
            if (inner) return inner;
          }
        }
        return null;
      };

      const allParts = msgData.payload?.parts || (msgData.payload ? [msgData.payload] : []);
      const attachment = findAttachment(allParts);
      if (!attachment) continue;

      // Attachment body may be inline (body.data) or referenced by id
      let csvBase64 = attachment.body?.data;
      if (!csvBase64 && attachment.body?.attachmentId) {
        const attRes = await fetch(
          `https://gmail.googleapis.com/gmail/v1/users/me/messages/${msg.id}/attachments/${attachment.body.attachmentId}`,
          { headers: authHeader }
        );
        if (attRes.ok) {
          const attData = await attRes.json();
          csvBase64 = attData.data;
        }
      }
      if (!csvBase64) continue;

      // Decode URL-safe base64 → UTF-8 string
      const binary = atob(csvBase64.replace(/-/g, '+').replace(/_/g, '/'));
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return new TextDecoder('utf-8').decode(bytes);
    }

    warnings.push(`Found ${messages.length} email(s) for ${label} but none had a ${filenamePrefix}*.csv attachment.`);
    return null;
  } catch (e) {
    warnings.push(`Gmail fetch for ${label} threw: ${e.message}`);
    return null;
  }
}

async function fetchReportsFromGmail(base44, warnings) {
  let accessToken;
  try {
    const conn = await base44.asServiceRole.connectors.getConnection('gmail');
    accessToken = conn.accessToken;
  } catch (e) {
    warnings.push(`Could not connect to Gmail: ${e.message}. Provide glReport and workOrderReport in payload to bypass.`);
    return { glReport: null, woReport: null };
  }
  const authHeader = { Authorization: `Bearer ${accessToken}` };

  // Match by sender domain + subject keywords + filename prefix.
  // Use 45-day window so a re-sent copy from earlier in the month is found.
  const [glReport, woReport] = await Promise.all([
    searchGmailForReport(
      authHeader,
      'from:donotreply@appfolio.com subject:"Rent Roll" newer_than:45d',
      'report_builder-',
      warnings,
      'GL Report'
    ),
    searchGmailForReport(
      authHeader,
      'from:donotreply@appfolio.com subject:"Work Order" newer_than:45d',
      'work_order-',
      warnings,
      'Work Order Report'
    )
  ]);

  return { glReport, woReport };
}

// ─────────────────────────────────────────────────────────────────────
// Date validation — all GL transaction dates must be within the
// expected reporting month. Halts (returns false) if most are wrong.
// ─────────────────────────────────────────────────────────────────────

function validateGLDates(glCsvText, period, warnings) {
  const rows = parseCsvSafely(glCsvText);
  const headerIdx = findHeaderRow(rows, ['date', 'gl account']);
  if (headerIdx === -1) return true; // can't validate, proceed
  const dateCol = colIndex(rows[headerIdx], 'date');
  if (dateCol === -1) return true;

  const expectedYear = period.year;
  const expectedMonth = period.month; // 0-based

  let total = 0, outside = 0;
  for (let i = headerIdx + 1; i < rows.length; i++) {
    const raw = (rows[i][dateCol] || '').toString().trim();
    if (!raw) continue;
    const d = new Date(raw);
    if (isNaN(d.getTime())) continue;
    total++;
    if (d.getFullYear() !== expectedYear || d.getMonth() !== expectedMonth) outside++;
  }

  if (total === 0) return true;
  const outsidePct = outside / total;

  if (outsidePct > 0.5) {
    warnings.push(
      `GL date validation FAILED: ${outside}/${total} transaction dates (${(outsidePct * 100).toFixed(0)}%) ` +
      `fall outside ${period.label}. The AppFolio date filter may have reset to blank. ` +
      `Reports have NOT been generated to avoid sending incorrect data. ` +
      `Re-export the GL Report in AppFolio with Date Range = "Last Month" and try again.`
    );
    return false;
  }
  if (outside > 0) {
    warnings.push(
      `GL date check: ${outside} of ${total} transactions fall outside ${period.label} ` +
      `(likely late-posting entries — proceeding, but verify the AppFolio date filter is set to "Last Month").`
    );
  }
  return true;
}

// ─────────────────────────────────────────────────────────────────────
// Owner email — primary source: hardcoded map below.
//
// HOW TO MAINTAIN:
//   Keys must match the Owner(s) string exactly as AppFolio reports it
//   in the Property Directory column. Whitespace and punctuation matter
//   for the exact-match path; the fuzzy fallback handles minor variants
//   (LLC vs L.L.C., extra spaces) but exact keys are safer.
//   Owner emails change rarely — updating this map once per change is
//   lower maintenance than any dynamic source.
//
// FALLBACK:
//   If an owner name isn't in the map, V9 falls back to the
//   DirectoryProperty entity (populated by V8 over months). This means
//   V9 works correctly before the map is fully populated and protects
//   against accidental omissions. Once the map is complete the entity
//   fallback becomes a no-op.
//
// NEVER overwrite a non-blank owner_email with blank — enforced in the
// sync block below regardless of which source the email came from.
// ─────────────────────────────────────────────────────────────────────

// ▼▼▼ FILL IN ACTUAL OWNER EMAILS BEFORE DEPLOYING ▼▼▼
// Key = Owner(s) value from AppFolio Property Directory, lowercased.
// Run V9 once against a real GL export and check the warnings log for
// any "no email found" messages — those owner names are your keys.
const OWNER_EMAIL_MAP: Record<string, string> = {
  // 'john smith llc':           'john@example.com',
  // 'jane doe':                 'jane@example.com',
};
// ▲▲▲

async function buildOwnerEmailMap(base44): Promise<Record<string, string>> {
  // Start with the hardcoded map (primary source)
  const map: Record<string, string> = {};
  for (const [k, v] of Object.entries(OWNER_EMAIL_MAP)) {
    if (k && v) map[k.toLowerCase().trim()] = v.trim();
  }

  // Merge entity data as fallback for any owner not yet in the hardcoded map
  try {
    const existingProps = await base44.asServiceRole.entities.DirectoryProperty.list(null, 1000);
    for (const p of (existingProps || [])) {
      if (p.owner_name && p.owner_email) {
        const key = p.owner_name.toLowerCase().trim();
        if (!map[key]) map[key] = p.owner_email.trim();  // only fill gaps, never overwrite hardcoded
      }
    }
  } catch (e) {
    console.error('buildOwnerEmailMap entity fallback failed:', e);
  }

  return map;
}

function getOwnerEmail(ownerName, emailMap, warnings) {
  if (!ownerName) return '';
  const lower = ownerName.toLowerCase().trim();
  if (emailMap[lower]) return emailMap[lower];
  // Fuzzy: strip non-alphanumeric and try partial containment
  const norm = (s: string) => s.replace(/[^a-z0-9]/g, '');
  const normLower = norm(lower);
  for (const [key, email] of Object.entries(emailMap)) {
    const normKey = norm(key);
    if (normKey && normLower && (normKey.includes(normLower) || normLower.includes(normKey))) {
      return email;
    }
  }
  if (ownerName) {
    warnings.push(
      `Owner "${ownerName}" has no email address — their report will be generated but cannot be sent automatically. ` +
      `Add their email to OWNER_EMAIL_MAP in buildInvestorReportsV9/entry.ts.`
    );
  }
  return '';
}

// ─────────────────────────────────────────────────────────────────────
// GL Report parser
// Parses: Rent Roll + GL transactions + Occupancy Summary +
//         Property Directory (Owner(s), Reserve)
// One row per unit per GL transaction — no cross-join risk.
// ─────────────────────────────────────────────────────────────────────

function parseGLReport(csvText, period, warnings) {
  const rows = parseCsvSafely(csvText);
  const properties = {};

  const headerIdx = findHeaderRow(rows, ['property', 'gl account']);
  if (headerIdx === -1) {
    warnings.push('GL Report: cannot find header row with "Property" and "GL Account" columns. Check that the correct file was uploaded/sent.');
    return properties;
  }
  const header = rows[headerIdx];

  const col = {
    property:      colIndex(header, 'property'),
    unit:          colIndex(header, 'unit'),
    tenant:        colIndex(header, 'tenant'),
    status:        colIndex(header, 'status'),
    total:         colIndex(header, 'total'),
    leaseTo:       colIndex(header, 'lease to'),
    moveOut:       colIndex(header, 'move-out'),
    glAccount:     colIndex(header, 'gl account'),
    date:          colIndex(header, 'date'),
    debit:         colIndex(header, 'debit'),
    credit:        colIndex(header, 'credit'),
    unitCount:     colIndex(header, '# of units'),
    occupied:      colIndex(header, 'occupied'),
    pctOccupied:   colIndex(header, '% occupied'),
    avgMarketRent: colIndex(header, 'average market rent'),
    // Property Directory columns
    ownerNames:    colIndex(header, 'owner(s)'),
    reserve:       colIndex(header, 'reserve'),
  };

  const ensureProp = (name) => {
    if (!properties[name]) {
      properties[name] = {
        income: 0,
        concessions: 0,
        distribution: 0,
        expenseItems: {},
        units: {},
        occupancy: { numUnits: null, occupied: null, pctOccupied: null, avgMarketRent: null },
        ownerName: null,
        reserve: 0,
      };
    }
    return properties[name];
  };

  for (let i = headerIdx + 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || !row.length) continue;
    const propCell = (row[col.property] || '').toString().trim();

    if (propCell.startsWith('->')) continue;
    if (!propCell) continue;
    if (/^\d+\s*units?$/i.test(propCell)) continue;
    if (propCell.toLowerCase() === 'total') continue;
    if (isExcludedProperty(propCell)) continue;

    const propName = cleanPropertyName(propCell);
    const p = ensureProp(propName);

    // Property Directory fields — property-level, repeated per row; capture once
    if (!p.ownerName && col.ownerNames !== -1) {
      const ownerCell = (row[col.ownerNames] || '').toString().trim();
      if (ownerCell) p.ownerName = ownerCell;
    }
    if (!p.reserve && col.reserve !== -1) {
      const reserveVal = parseMoney(row[col.reserve]);
      if (reserveVal) p.reserve = reserveVal;
    }

    // Occupancy Summary — property-level, capture once
    if (p.occupancy.numUnits === null && col.unitCount !== -1 && row[col.unitCount]) {
      p.occupancy.numUnits   = parseInt(row[col.unitCount], 10) || null;
      p.occupancy.occupied   = parseInt(row[col.occupied],  10) || 0;
      p.occupancy.pctOccupied  = (row[col.pctOccupied]   || '').toString().trim();
      p.occupancy.avgMarketRent = parseMoney(row[col.avgMarketRent]);
    }

    // Rent Roll / unit identity fields
    const tenant = (row[col.tenant] || '').toString().trim();
    const unitKey = (row[col.unit] || '').toString().trim() || tenant || 'Main';
    if (tenant || (row[col.status] || '').toString().trim()) {
      const status    = (row[col.status]  || '').toString().trim();
      const leaseTo   = (row[col.leaseTo] || '').toString().trim();
      const moveOut   = (row[col.moveOut] || '').toString().trim();
      const rentAmt   = parseMoney(row[col.total]);
      const statusL   = status.toLowerCase();
      const onNotice  = statusL.includes('notice') || !!moveOut;
      const isVacant  = !tenant || statusL.includes('vacant');
      if (!p.units[unitKey]) {
        p.units[unitKey] = {
          tenant: isVacant ? 'VACANT' : tenant,
          status,
          lease:    moveOut || leaseTo || 'N/A',
          onNotice: !isVacant && onNotice,
          rentAmt,
        };
      }
    }

    // GL transaction line
    const glCell = (row[col.glAccount] || '').toString().trim();
    if (glCell && col.debit !== -1 && col.credit !== -1) {
      const debit  = parseMoney(row[col.debit]);
      const credit = parseMoney(row[col.credit]);
      const net    = credit - debit;
      if (Math.abs(net) > 0.001) {
        const cls = classifyGLAccount(glCell);
        if (cls.unrecognized) {
          warnings.push(
            `GL Report: account "${glCell}" is not in the 32-account whitelist — ` +
            `classified as ${cls.category}/${cls.subcategory || 'maintenance'} by name match. ` +
            `Update the INCOME/MAINTENANCE/OPERATING_SUBCATEGORY maps in this file if this account is expected.`
          );
        }
        if (cls.type === 'income') {
          p.income += net;
          if (cls.isConcession && net < 0) p.concessions += Math.abs(net);
        } else if (cls.type === 'distribution') {
          // CONFIRM: sign convention on 3250 not yet verified against real data.
          // Using Math.abs(net) as a safe default until a real distribution appears.
          p.distribution += Math.abs(net);
        } else if (cls.type === 'expense') {
          const key = cls.name || glCell;
          if (!p.expenseItems[key]) {
            p.expenseItems[key] = { amount: 0, category: cls.category, subcategory: cls.subcategory };
          }
          p.expenseItems[key].amount += Math.abs(net);
        }
      }
    }
  }

  return properties;
}

// ─────────────────────────────────────────────────────────────────────
// Work Order Report parser (plain AppFolio report, not Report Builder)
// ─────────────────────────────────────────────────────────────────────

function parseWorkOrderReport(csvText, warnings) {
  const rows = parseCsvSafely(csvText);
  const properties = {};

  const headerIdx = findHeaderRow(rows, ['property', 'work order number']);
  if (headerIdx === -1) {
    warnings.push('Work Order Report: cannot find header row. Check that the correct file was uploaded/sent.');
    return properties;
  }
  const header = rows[headerIdx];

  const col = {
    property:    colIndex(header, 'property'),
    unit:        colIndex(header, 'unit'),
    desc:        colIndex(header, 'job description'),
    status:      colIndex(header, 'status'),
    amount:      colIndex(header, 'amount'),
  };

  for (let i = headerIdx + 1; i < rows.length; i++) {
    const row = rows[i];
    if (!row || !row.length) continue;
    const propCell = (row[col.property] || '').toString().trim();
    if (propCell.startsWith('->') || !propCell || propCell.toLowerCase() === 'total') continue;
    if (isExcludedProperty(propCell)) continue;

    const desc = (row[col.desc] || '').toString().trim();
    if (!desc) continue;

    const propName  = cleanPropertyName(propCell);
    const unit      = (row[col.unit]   || '').toString().trim() || 'Main';
    const amount    = Math.abs(parseMoney(row[col.amount]));
    const status    = (row[col.status] || '').toString().trim() || 'Completed';

    if (!properties[propName]) properties[propName] = [];
    const isDupe = properties[propName].some(w => w.desc === desc && Math.abs(w.amount - amount) < 0.01);
    if (!isDupe) properties[propName].push({ unit, desc, amount, status });
  }

  return properties;
}

// ─────────────────────────────────────────────────────────────────────
// HTML rendering (identical look to V8 — data pipeline changed, not UI)
// ─────────────────────────────────────────────────────────────────────

const EXPENSE_TRANSLATIONS = {
  'full renovation': 'שיפוץ מלא', 'plumbing': 'אינסטלציה', 'appliances': 'מוצרי חשמל',
  'landscaping': 'גינון', 'pest control': 'הדברה', 'electricity': 'חשמל', 'water': 'מים',
  'garbage and recycling': 'פינוי אשפה', 'management fees': 'דמי ניהול',
  'commissions/placement fees': 'עמלת שיווק', 'repairs - other': 'תיקונים שונים',
  'inspection': 'ביקורת נכס', 'property insurance': 'תשלום ביטוח', 'legal': 'הוצאות משפטיות',
  'hoa dues': 'דמי ועד בית', 'painting': 'צביעה', 'hvac (heat, ventilation, air)': 'מיזוג אוויר',
  'roof repair': 'תיקון גג', 'flooring': 'ריצוף', 'septic/well': 'ביוב / בור ספיגה',
  'rodents treatment': 'הדברת מכרסמים', 'termites treatment': 'הדברת טרמיטים',
  'contractor pay': 'תשלום לקבלן', 'property tax': 'ארנונה', 'leasing fee': 'עמלת השכרה',
  'supplies': 'ציוד וחומרים', 'key/lock replacement': 'החלפת מנעולים',
  'painting': 'צביעה', 'repairs-other': 'תיקונים שונים',
};
const SUBCAT_TRANSLATIONS = {
  'Management & Fees': 'ניהול ועמלות',
  'Utilities': 'שירותים ציבוריים',
  'Insurance, Legal & Other': 'ביטוח, משפטי ואחר',
  'Other Operating': 'הוצאות תפעוליות אחרות',
};

const translateExpense = (n, isHe) => isHe ? (EXPENSE_TRANSLATIONS[n.toLowerCase().trim()] || n) : n;
const translateSubcat  = (n, isHe) => isHe ? (SUBCAT_TRANSLATIONS[n] || n) : n;

function fmtMoney(val, negative) {
  const abs = Math.abs(val).toLocaleString(undefined, { minimumFractionDigits: 2 });
  return `<span dir="ltr">${negative ? '-' : ''}$${abs}</span>`;
}

function buildHtmlReport(owner, periodString, logo, watermark, generationDate, globalWarnings, lang) {
  const isHe = lang === 'he';
  const dir  = isHe ? 'rtl' : 'ltr';

  const L = {
    reportTitle:   isHe ? 'דוח ביצועים חודשי'                : 'Monthly Portfolio Report',
    ownerLabel:    isHe ? 'בעלים'                              : 'Owner',
    periodLabel:   isHe ? 'תקופה'                              : 'Period',
    generatedLabel:isHe ? 'הופק בתאריך'                       : 'Generated',
    sec1:          isHe ? 'א. השכרה ותפוסה'                   : 'I.- Leasing & Occupancy',
    sec2:          isHe ? 'ב. תחזוקה ותפעול'                  : 'II.- Maintenance & Operations',
    sec3:          isHe ? 'ג. ביצועים פיננסיים'               : 'III.- Financial Performance',
    occupancy:     isHe ? 'תפוסה'                              : 'Occupancy',
    netIncome:     isHe ? 'הכנסה נטו'                         : 'Net Income Received',
    expenseRatio:  isHe ? 'יחס הוצאות'                        : 'Expense Ratio',
    maintSpend:    isHe ? 'הוצאות תחזוקה'                     : 'Maintenance Spend',
    maintExpenses: isHe ? 'פירוט הוצאות תחזוקה'              : 'Maintenance Expenses',
    workOrders:    isHe ? 'הוראות עבודה'                      : 'Work Orders',
    noActivity:    isHe ? 'אין פעילות לתקופה זו'             : 'No recent activity.',
    leaseEnds:     isHe ? 'חוזה עד'                           : 'Lease ends',
    movesOut:      isHe ? 'עוזב בתאריך'                       : 'Moves out',
    vacant:        isHe ? 'פנוי'                               : 'VACANT',
    notice:        isHe ? 'הודעת עזיבה'                       : 'NOTICE',
    totalIncome:   isHe ? 'סה"כ הכנסות שהתקבלו'             : 'Total Income Received',
    concessions:   isHe ? 'הנחות לדיירים (כלול לעיל)'       : 'Tenant Concessions (included above)',
    totalExpenses: isHe ? 'סה"כ הוצאות ששולמו'              : 'Total Expenses Paid',
    maintRow:      isHe ? "תחזוקה ותיקונים (ראה סעיף ב')"   : 'Maintenance & Repairs (see Section II)',
    noi:           isHe ? 'רווח תפעולי נטו (NOI)'            : 'Net Operating Income',
    distribution:  isHe ? 'חלוקה לבעלים החודש'               : 'Owner Distribution This Month',
    reserve:       isHe ? 'רזרבה נדרשת'                      : 'Required Reserve',
    portalNote:    isHe
      ? '📊 דוח זה מציג את הפעילות התפעולית של הנכס לחודש הנוכחי — תשלומים שהתקבלו, הוצאות ששולמו, תחזוקה ותפוסה. לנתונים חשבונאיים מלאים, יש להיכנס לפורטל המשקיעים של AppFolio.'
      : '📊 This report presents the operational performance of your property for the current month — income received, expenses paid, maintenance activity, and occupancy. For full accounting data, please refer to your AppFolio Investor Portal.',
  };

  const portfolioHtml = owner.portfolio.map(p => {
    const leaseCards = Object.entries(p.units).map(([name, u]) => {
      const isVacant  = u.tenant === 'VACANT';
      const cardClass = isVacant ? 'vacant' : (u.onNotice ? 'notice' : 'occupied');
      const noticeBadge = u.onNotice && !isVacant ? `<div class="notice-badge">&#128276; ${L.notice}</div>` : '';
      const dateLabel   = u.onNotice && !isVacant
        ? `${L.movesOut}: <b>${u.lease}</b>`
        : (isVacant ? u.lease : `${L.leaseEnds}: ${u.lease}`);
      return `<div class="card mini ${cardClass}"><div class="label">${name}</div><div class="val">${isVacant ? L.vacant : u.tenant}</div><div class="sub">${dateLabel}</div>${noticeBadge}</div>`;
    }).join('');

    const maintItems = p.workOrders.slice(0, 10).map(wo =>
      `<div class="wo-row"><span class="pill">${wo.status}</span><span class="amt">${fmtMoney(wo.amount, false)}</span><div class="desc"><b>${wo.unit}:</b> ${wo.desc}</div></div>`
    ).join('') || `<div style="color:#94a3b8;font-size:11px;">${L.noActivity}</div>`;

    const concessionsNote = p.financials.concessions > 0
      ? `<tr class="concession-row"><td>&nbsp;&nbsp;&nbsp;&nbsp;<i>&#10003; ${L.concessions}</i></td><td class="r" style="color:#dc2626;font-style:italic;">${fmtMoney(p.financials.concessions, true)}</td></tr>`
      : '';

    return `
    <div class="prop-wrap">
      <h2 style="background-color:#3b0764;color:#fff;padding:15px;border-radius:8px;text-align:center;margin:20px 0;">${p.name}</h2>
      <div class="grid">
        <div class="bubble">
          <div class="bt">${L.sec1}</div>
          <div class="top-stats">
            <div class="stat"><div class="v">${p.metrics.occRate}%</div><div class="l">${L.occupancy}</div></div>
            <div class="stat"><div class="v">${fmtMoney(p.financials.income, false)}</div><div class="l">${L.netIncome}</div></div>
          </div>
          <div class="card-grid">${leaseCards}</div>
        </div>
        <div class="bubble">
          <div class="bt">${L.sec2}</div>
          <div class="top-stats">
            <div class="stat highlight"><div class="v">${p.metrics.expenseRatio}</div><div class="l">${L.expenseRatio}</div></div>
            <div class="stat"><div class="v">${fmtMoney(p.maintenanceTotal || 0, false)}</div><div class="l">${L.maintSpend}</div></div>
          </div>
          ${p.maintenanceItems.length ? `<div class="maint-section"><div class="maint-label">${L.maintExpenses}</div>${p.maintenanceItems.map(it => `<div class="maint-line"><span class="maint-name">${translateExpense(it.name, isHe)}</span><span class="maint-amt">${fmtMoney(it.amount, true)}</span></div>`).join('')}</div>` : ''}
          ${p.workOrders.length ? `<div class="maint-label" style="margin-top:12px;">${L.workOrders}</div><div class="wo-box">${maintItems}</div>` : ''}
        </div>
        <div class="bubble full">
          <div class="bt">${L.sec3}</div>
          <table class="ft">
            <tr><td>${L.totalIncome}</td><td class="r">${fmtMoney(p.financials.income, false)}</td></tr>
            ${concessionsNote}
            <tr class="p"><td>${L.totalExpenses}</td><td class="r">${fmtMoney(p.financials.expense, true)}</td></tr>
            ${p.maintenanceTotal > 0 ? `<tr class="exp-item"><td>&nbsp;&nbsp;&nbsp;&nbsp;&bull; ${L.maintRow}</td><td class="r">${fmtMoney(p.maintenanceTotal, true)}</td></tr>` : ''}
            ${p.operatingSummary.map(it => `<tr class="exp-item"><td>&nbsp;&nbsp;&nbsp;&nbsp;&bull; ${translateSubcat(it.name, isHe)}</td><td class="r">${fmtMoney(it.amount, true)}</td></tr>`).join('')}
            <tr class="m"><td>${L.noi}</td><td class="r">${fmtMoney(p.financials.noi, false)}</td></tr>
            <tr class="p"><td>${L.distribution}</td><td class="r">${fmtMoney(p.balances.distribution, true)}</td></tr>
          </table>
          ${p.reserve > 0 ? `<div class="reserve-row"><span>${L.reserve}: ${fmtMoney(p.reserve, false)}</span></div>` : ''}
          <div class="portal-note">${L.portalNote}</div>
          ${(isHe ? p.aiSummaryHe : p.aiSummaryEn) ? `<div class="ai-summary"><div class="ai-summary-label">✦ ${isHe ? 'סיכום חודשי' : 'Monthly Summary'}</div><p class="ai-summary-text">${isHe ? p.aiSummaryHe : p.aiSummaryEn}</p></div>` : ''}
        </div>
      </div>
    </div><div class="pb"></div>`;
  }).join('');

  const warningBanner = globalWarnings.length
    ? `<div style="background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:12px;border-radius:8px;margin-bottom:20px;font-size:12px;"><b>&#9888;&#65039; Data Warning:</b> ${globalWarnings.join(' ')}</div>`
    : '';

  return `<html><head><meta charset="UTF-8"><style>
    @media print { * { -webkit-print-color-adjust: exact !important; } }
    body { font-family: 'Segoe UI', sans-serif; color: #0f172a; margin: 0; padding: 30px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    .bubble { background: #fff; border: 1px solid #e2e8f0; border-radius: 20px; padding: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); position: relative; }
    .full { grid-column: span 2; }
    .bt { color: #3b0764; font-weight: 900; font-size: 14px; border-bottom: 3px solid #d946ef; padding-bottom: 8px; margin-bottom: 15px; text-transform: uppercase; }
    .top-stats { display: flex; gap: 10px; margin-bottom: 20px; }
    .stat { background: #f8fafc; flex: 1; padding: 12px; border-radius: 12px; text-align: center; }
    .stat.highlight { background: #fdf2f8; }
    .stat .v { font-size: 20px; font-weight: bold; color: #d946ef; }
    .stat .l { font-size: 9px; text-transform: uppercase; color: #64748b; font-weight: 700; }
    .card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .card.mini { padding: 10px; border-radius: 10px; border: 1px solid #f1f5f9; border-left: 5px solid #d946ef; position: relative; }
    .card.vacant { background: #fff1f2; border-left-color: #f43f5e; }
    .card.notice { background: #fff7ed; border-left-color: #ea580c; }
    .card.notice .val { color: #9a3412; }
    .card.notice .sub { color: #9a3412; font-weight: 700; }
    .notice-badge { position: absolute; top: 6px; right: 6px; background: #ea580c; color: #fff; font-size: 7px; font-weight: 900; padding: 2px 5px; border-radius: 4px; }
    .label { font-size: 8px; color: #94a3b8; font-weight: bold; }
    .val { font-size: 11px; font-weight: bold; margin: 3px 0; }
    .sub { font-size: 8px; color: #64748b; }
    .wo-row { padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 10px; }
    .pill { background: #fbbf24; color: #3b0764; padding: 2px 6px; border-radius: 6px; font-size: 8px; font-weight: 900; }
    .amt { float: right; font-weight: bold; }
    .ft { width: 100%; border-collapse: collapse; }
    .ft td { padding: 10px; border-bottom: 1px solid #f1f5f9; font-size: 14px; }
    .ft .r { text-align: right; font-weight: bold; }
    .ft .m { color: #d946ef; font-weight: 800; }
    .ft .exp-item td { font-size: 11px; color: #64748b; padding: 4px 10px 4px 20px; border-bottom: none; }
    .ft .exp-item td.r { font-weight: 600; color: #475569; }
    .ft .concession-row td { font-size: 11px; color: #dc2626; padding: 3px 10px 3px 20px; border-bottom: none; font-style: italic; }
    .maint-section { margin-top: 5px; }
    .maint-label { font-size: 10px; color: #3b0764; font-weight: 800; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid #f1f5f9; }
    .maint-line { display: flex; justify-content: space-between; padding: 5px 0; font-size: 11px; border-bottom: 1px dotted #f1f5f9; }
    .maint-name { color: #475569; }
    .maint-amt { color: #be185d; font-weight: 700; }
    .reserve-row { display: flex; justify-content: flex-end; margin-top: 12px; padding: 10px 15px; background: #f8fafc; border-radius: 8px; font-size: 12px; color: #64748b; font-weight: 600; }
    .portal-note { margin-top: 12px; padding: 12px 16px; background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 10px; font-size: 11px; color: #0369a1; line-height: 1.8; }
    .ai-summary { margin-top: 14px; padding: 16px 20px; background: linear-gradient(135deg, #fdf4ff 0%, #faf5ff 100%); border: 1px solid #e9d5ff; border-left: 4px solid #d946ef; border-radius: 10px; }
    .ai-summary-label { font-size: 10px; font-weight: 900; color: #7e22ce; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
    .ai-summary-text { font-size: 12px; color: #374151; line-height: 1.8; margin: 0; font-style: italic; }
    .pb { page-break-after: always; }
    .prop-wrap { position: relative; }
    .prop-wrap::after { content:''; position: absolute; top: 10%; left: 10%; width: 80%; height: 80%; background: url('${watermark}') no-repeat center; background-size: contain; opacity: 0.04; z-index: -1; }
  </style></head><body dir="${dir}">
    <table style="width:100%;border-bottom:3px solid #3b0764;margin-bottom:20px;padding-bottom:15px;">
      <tr>
        <td style="width:50%;vertical-align:bottom;"><img src="${logo}" width="200"></td>
        <td style="width:50%;text-align:${isHe ? 'left' : 'right'};vertical-align:bottom;font-size:14px;color:#0f172a;line-height:1.6;">
          <b style="font-size:17px;color:#3b0764;">${L.reportTitle}</b><br>
          <span style="color:#64748b;">${L.ownerLabel}:</span> <b>${owner.name}</b><br>
          <span style="color:#64748b;">${L.periodLabel}:</span> <b>${periodString}</b><br>
          <span style="color:#64748b;font-size:10px;">${L.generatedLabel}:</span> <span style="font-size:10px;color:#94a3b8;">${generationDate}</span>
        </td>
      </tr>
    </table>
    ${warningBanner}
    ${portfolioHtml}
  </body></html>`;
}

// ─────────────────────────────────────────────────────────────────────
// Period helper
// ─────────────────────────────────────────────────────────────────────

function getReportPeriod() {
  const now = new Date();
  const d   = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  return {
    label: new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric' }).format(d),
    month: d.getMonth(),
    year:  d.getFullYear(),
    start: new Date(d.getFullYear(), d.getMonth(), 1),
    end:   new Date(d.getFullYear(), d.getMonth() + 1, 0),
  };
}

// ─────────────────────────────────────────────────────────────────────
// Main orchestrator
// ─────────────────────────────────────────────────────────────────────

async function doBuildReports(payload, base44) {
  const period   = getReportPeriod();
  const warnings = [];

  const logoUrl      = 'https://www.dropbox.com/scl/fo/zka31q1g6xh7zmfrylyew/AMGvFe5rppJfWVkqlAHF2GI/Artboard%201%20copy%203%403x-100.jpg?rlkey=h8cqdxc3w51ieu34oy2vcxhp8&raw=1';
  const watermarkUrl = 'https://www.dropbox.com/scl/fo/zka31q1g6xh7zmfrylyew/AHpWwmkRsrCDglhslgDtjN8/Artboard%201%20copy%204%403x-8.png?rlkey=h8cqdxc3w51ieu34oy2vcxhp8&raw=1';

  // ── Step 1: Get CSVs from payload (manual override) or Gmail ──────
  let glCsvText = payload?.glReport     || null;
  let woCsvText = payload?.workOrderReport || null;

  if (!glCsvText || !woCsvText) {
    const gmailReports = await fetchReportsFromGmail(base44, warnings);
    glCsvText  = glCsvText  || gmailReports.glReport;
    woCsvText  = woCsvText  || gmailReports.woReport;
  }

  if (!glCsvText) {
    warnings.push('GL Report CSV is missing — no financial data available. Provide payload.glReport or configure Gmail to receive AppFolio scheduled reports.');
  }
  if (!woCsvText) {
    warnings.push('Work Order Report CSV is missing — maintenance job list will be empty. Provide payload.workOrderReport or configure Gmail.');
  }

  // ── Step 2: Date validation (guard against blank filter in AppFolio) ─
  if (glCsvText) {
    const datesOk = validateGLDates(glCsvText, period, warnings);
    if (!datesOk) {
      // Critical: dates are completely wrong. Return early with warnings only.
      return [{ ownerName: 'DATA ERROR', ownerEmail: '', pdfUrl: '', htmlContent: '', warnings: warnings.join(' ') }];
    }
  }

  // ── Step 3: Parse both reports ────────────────────────────────────
  const glProperties = parseGLReport(glCsvText || '', period, warnings);
  const woProperties = parseWorkOrderReport(woCsvText || '', warnings);

  // ── Step 4: Owner email map (hardcoded + entity fallback) ───────────
  const ownerEmailMap = await buildOwnerEmailMap(base44);

  // ── Step 5: Group properties by owner name ────────────────────────
  // Owner-property relationship comes from the GL Report's Owner(s)
  // column (Property Directory integration). No Owner Directory CSV needed.
  const ownerGroups = {};  // ownerName -> [propName]
  for (const [propName, propData] of Object.entries(glProperties)) {
    if (!propData.ownerName) {
      warnings.push(`Property "${propName}": blank Owner(s) in the GL Report — skipping (unmanaged property or data entry issue in AppFolio).`);
      continue;
    }
    if (!ownerGroups[propData.ownerName]) ownerGroups[propData.ownerName] = [];
    ownerGroups[propData.ownerName].push(propName);
  }

  // ── Step 6: Build per-owner portfolio objects ─────────────────────
  const allOwners = [];

  for (const [ownerName, propNames] of Object.entries(ownerGroups)) {
    const ownerObj = { name: ownerName, portfolio: [] };

    for (const pName of propNames) {
      const gl = glProperties[pName];
      if (!gl) continue;

      // Work orders for this property (descriptive only, NOT summed into financials)
      const workOrders = Object.keys(woProperties)
        .filter(k => propertiesMatch(k, pName))
        .flatMap(k => woProperties[k]);

      // Financial aggregation from GL
      const income      = gl.income;
      const concessions = gl.concessions;
      const distribution = gl.distribution;

      const expenseItemsArr = Object.entries(gl.expenseItems)
        .map(([name, v]) => ({ name, amount: v.amount, category: v.category, subcategory: v.subcategory }));

      const maintenanceItems  = expenseItemsArr.filter(it => it.category === 'maintenance').sort((a, b) => b.amount - a.amount);
      const operatingItems    = expenseItemsArr.filter(it => it.category === 'operating');
      const maintenanceTotal  = maintenanceItems.reduce((s, it) => s + it.amount, 0);
      const operatingTotal    = operatingItems.reduce((s, it) => s + it.amount, 0);

      const opSubMap = {};
      operatingItems.forEach(it => {
        const k = it.subcategory || 'Other Operating';
        opSubMap[k] = (opSubMap[k] || 0) + it.amount;
      });
      const operatingSummary = Object.entries(opSubMap)
        .map(([name, amount]) => ({ name, amount }))
        .sort((a, b) => b.amount - a.amount);

      const expense = maintenanceTotal + operatingTotal;
      const noi     = income - expense;

      // Occupancy — prefer Occupancy Summary column if present, else count from units
      const units     = gl.units;
      const unitsArr  = Object.values(units);
      const occCount  = unitsArr.filter(u => u.tenant && u.tenant !== 'VACANT').length;
      const occRateValue = unitsArr.length
        ? ((occCount / unitsArr.length) * 100).toFixed(0)
        : (gl.occupancy.pctOccupied ? parseFloat(gl.occupancy.pctOccupied) : 0);

      ownerObj.portfolio.push({
        name: pName,
        reserve: gl.reserve,
        financials: { income, expense, noi, concessions },
        balances:   { distribution },
        metrics: {
          occRate: occRateValue,
          expenseRatio: income > 0 ? ((expense / income) * 100).toFixed(1) + '%' : 'N/A',
        },
        units,
        workOrders,
        maintenanceItems,
        operatingSummary,
        maintenanceTotal,
        operatingTotal,
        aiSummaryEn: '',
        aiSummaryHe: '',
      });
    }

    if (ownerObj.portfolio.length) allOwners.push(ownerObj);
  }

  // ── Step 7: LLM — summarise long work order descriptions ──────────
  const uniqueLongDescs = new Set();
  allOwners.forEach(o => o.portfolio.forEach(p =>
    p.workOrders.forEach(w => { if (w.desc && w.desc.length > 30) uniqueLongDescs.add(w.desc); })
  ));

  if (uniqueLongDescs.size > 0 && base44) {
    try {
      const descArray = Array.from(uniqueLongDescs);
      const prompt = `Summarize each maintenance work order description into 3-5 words max (e.g. "Clogged toilet", "AC repair"). Return JSON: keys = exact original descriptions, values = short summaries.\nDescriptions: ${JSON.stringify(descArray)}`;
      const summarizedMap = await base44.asServiceRole.integrations.Core.InvokeLLM({
        prompt,
        response_json_schema: { type: 'object', additionalProperties: { type: 'string' } },
        model: 'gpt_5_mini'
      });
      if (summarizedMap) {
        allOwners.forEach(o => o.portfolio.forEach(p =>
          p.workOrders.forEach(w => {
            if (summarizedMap[w.desc]) w.desc = summarizedMap[w.desc];
            else if (w.desc && w.desc.length > 30) w.desc = w.desc.substring(0, 30) + '...';
          })
        ));
      }
    } catch (e) { console.error('Work order summarisation failed:', e); }
  }

  // ── Step 8: LLM — AI monthly summary (EN + HE) ───────────────────
  const generationDate = new Date().toLocaleString('en-US', { timeZone: 'America/New_York' });

  if (base44) {
    try {
      const propSnapshots = [];
      allOwners.forEach(o => o.portfolio.forEach(p => {
        const key = `${o.name}||${p.name}`;
        const vacantUnits = Object.values(p.units).filter(u => u.tenant === 'VACANT').length;
        const totalUnits  = Object.keys(p.units).length;
        const onNotice    = Object.values(p.units).filter(u => u.onNotice && u.tenant !== 'VACANT').length;
        propSnapshots.push({
          key, property: p.name, owner: o.name, period: period.label,
          totalUnits, occupiedUnits: totalUnits - vacantUnits, vacantUnits,
          unitsOnNotice: onNotice,
          rentCollected: `$${p.financials.income.toLocaleString()}`,
          totalExpenses: `$${p.financials.expense.toLocaleString()}`,
          ownerDistribution: `$${p.balances.distribution.toLocaleString()}`,
          leasingIncentiveGiven: p.financials.concessions > 0,
          leasingIncentiveAmount: p.financials.concessions > 0 ? `$${p.financials.concessions.toLocaleString()}` : null,
          maintenanceWork: p.maintenanceItems.slice(0, 5).map(it => `${it.name} ($${Math.abs(it.amount).toFixed(0)})`),
          workOrdersCompleted: p.workOrders.map(w => `${w.unit}: ${w.desc}`),
          expensesHigherThanIncome: p.financials.expense > p.financials.income,
          ownerReceivedPayment: p.balances.distribution > 0,
        });
      }));

      if (propSnapshots.length > 0) {
        const dataJson = JSON.stringify(propSnapshots);
        const enPrompt = `You are a senior property manager writing a personal monthly update letter to a real estate investor.
The investor may not be financially sophisticated — write as if explaining to a smart friend, not an accountant.
YOUR JOB: Help the investor understand what their numbers actually mean this month. Don't just name figures — explain them.
STRICT RULES:
- Write exactly 2 short paragraphs separated by a blank line.
- Paragraph 1: occupancy and rent collection, was it a good month and why.
- Paragraph 2: what was spent and why, plain language, one-time vs recurring, end forward-looking.
- Use proper punctuation, full sentences only.
- NEVER mention: management fees, NOI, expense ratio, concessions, distributions, accounting terms.
- Do not list numbers already visible in the report - interpret them instead.
- Max 5 sentences total.
Return ONLY a valid JSON object. Keys = exact "key" field. Values = the summary text. No preamble, no markdown.
Properties data:\n${dataJson}`;

        const hePrompt = `אתה מנהל נכסים בכיר שכותב מכתב עדכון חודשי אישי למשקיע נדל"ן.
המשקיע לא בהכרח בקיא בפיננסים — כתוב כאילו אתה מסביר לחבר חכם.
- כתוב בדיוק 2 פסקאות קצרות, מופרדות בשורה ריקה.
- פסקה ראשונה: תפוסה וגבייה. פסקה שנייה: מה הוצא ולמה, סיים במשפט מבט קדימה.
- אסור לציין: דמי ניהול, עמלות, NOI, יחס הוצאות, ז'רגון חשבונאי.
- מקסימום 5 משפטים סה"כ. עברית תקנית בלבד.
החזר JSON תקני בלבד. מפתחות = שדה "key". ערכים = טקסט הסיכום.
נתוני נכסים:\n${dataJson}`;

        const [enMap, heMap] = await Promise.all([
          base44.asServiceRole.integrations.Core.InvokeLLM({ prompt: enPrompt, response_json_schema: { type: 'object', additionalProperties: { type: 'string' } }, model: 'gpt_5_mini' }).catch(e => { console.error('EN summary failed:', e); return {}; }),
          base44.asServiceRole.integrations.Core.InvokeLLM({ prompt: hePrompt, response_json_schema: { type: 'object', additionalProperties: { type: 'string' } }, model: 'gpt_5_mini' }).catch(e => { console.error('HE summary failed:', e); return {}; }),
        ]);

        allOwners.forEach(o => o.portfolio.forEach(p => {
          const key = `${o.name}||${p.name}`;
          p.aiSummaryEn = (enMap && enMap[key]) || '';
          p.aiSummaryHe = (heMap && heMap[key]) || '';
        }));
      }
    } catch (e) { console.error('AI summary generation failed:', e); }
  }

  // ── Step 9: Auto-sync DirectoryProperty entity ────────────────────
  // CRITICAL: Never overwrite a non-blank owner_email with blank.
  // V8 has been populating these for months; treat existing data as
  // authoritative when V9 has no new email value.
  if (base44) {
    try {
      const existingProps = await base44.asServiceRole.entities.DirectoryProperty.list(null, 1000);
      const syncPromises  = [];

      allOwners.forEach(ownerObj => {
        const newEmail = getOwnerEmail(ownerObj.name, ownerEmailMap, []);  // warnings already logged in Step 4

        ownerObj.portfolio.forEach(p => {
          const unitKeys = Object.keys(p.units);

          unitKeys.forEach(unitName => {
            const unitData   = p.units[unitName] || {};
            const searchUnit = unitName === 'Main' ? '' : unitName;

            const match = existingProps.find(e => {
              const norm  = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
              const nAddr = norm(e.address);
              const nSearch = norm(p.name);
              const addrMatch = nAddr.includes(nSearch) || nSearch.includes(nAddr);
              if (!addrMatch) return false;
              const nEUnit = norm(e.unit);
              const nSUnit = norm(searchUnit);
              if (!nEUnit && !nSUnit) return true;
              if (nEUnit && nSUnit && (nEUnit.includes(nSUnit) || nSUnit.includes(nEUnit))) return true;
              if (!nEUnit && nSUnit && nAddr.includes(nSUnit)) return true;
              return false;
            });

            let propStatus = 'Vacant';
            if (unitData.tenant && unitData.tenant !== 'VACANT') {
              propStatus = unitData.onNotice ? 'Maintenance' : 'Occupied';
            }
            let rent = unitData.rentAmt || 0;
            if (!rent && unitKeys.length === 1) rent = p.financials.income || 0;

            const updateData = {
              owner_name:  ownerObj.name,
              tenant_name: unitData.tenant !== 'VACANT' ? (unitData.tenant || '') : '',
              status:      propStatus,
              available_date: unitData.lease || '',
              rent,
            };

            // Only set owner_email when we have a non-blank value.
            // If we have no email, leave the field alone — do NOT clear it.
            if (newEmail) updateData.owner_email = newEmail;
            // If the existing record has no email but we still have none,
            // the field is simply not touched (updateData has no owner_email key).

            if (match) {
              syncPromises.push(base44.asServiceRole.entities.DirectoryProperty.update(match.id, updateData));
            } else {
              syncPromises.push(base44.asServiceRole.entities.DirectoryProperty.create({
                address: searchUnit ? `${p.name} #${searchUnit}` : p.name,
                unit:    searchUnit,
                ...updateData,
              }));
            }
          });
        });
      });

      await Promise.allSettled(syncPromises);
    } catch (e) { console.error('DirectoryProperty sync failed:', e); }
  }

  // ── Step 10: Render HTML reports ──────────────────────────────────
  const heMonths = ['ינואר','פברואר','מרץ','אפריל','מאי','יוני','יולי','אוגוסט','ספטמבר','אוקטובר','נובמבר','דצמבר'];
  const hePeriodLabel = `${heMonths[period.month]} ${period.year}`;
  const uiResults = [];

  allOwners.forEach(ownerObj => {
    const ownerEmail = getOwnerEmail(ownerObj.name, ownerEmailMap, warnings);

    const htmlEn = buildHtmlReport(ownerObj, period.label, logoUrl, watermarkUrl, generationDate, warnings, 'en');
    const htmlHe = buildHtmlReport(ownerObj, hePeriodLabel, logoUrl, watermarkUrl, generationDate, warnings, 'he');

    const heNotice = `<div style="background:#f0f9ff;border:1px solid #bae6fd;color:#0369a1;text-align:center;padding:10px 20px;border-radius:8px;margin-bottom:20px;font-size:12px;">📄 Hebrew version of this report appears below &nbsp;|&nbsp; גרסה בעברית מופיעה בהמשך הדוח</div>`;
    const extractBody = (html, dir) => {
      const m = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
      return m ? `<div dir="${dir}" style="font-family:'Segoe UI',sans-serif;">${m[1]}</div>` : html;
    };
    const cssMatch = htmlEn.match(/<style>([\s\S]*?)<\/style>/i);
    const sharedCss = cssMatch ? cssMatch[1] : '';
    const divider = `<div class="pb"></div><div style="background:#3b0764;color:#fff;text-align:center;padding:18px;border-radius:8px;margin:30px 0;font-size:16px;font-weight:900;letter-spacing:1px;">גרסה בעברית &nbsp;|&nbsp; Hebrew Version</div>`;

    const combinedHtml = `<html><head><meta charset="UTF-8"><style>${sharedCss}
      .rtl-section .card.mini { border-left: none; border-right: 5px solid #d946ef; }
      .rtl-section .card.vacant { border-right-color: #f43f5e; }
      .rtl-section .card.notice { border-right-color: #ea580c; }
      .rtl-section .notice-badge { right: auto; left: 6px; }
      .rtl-section .amt { float: left; }
      .rtl-section .maint-line { flex-direction: row-reverse; }
      .rtl-section .reserve-row { flex-direction: row-reverse; }
    </style></head><body>
      <div class="ltr-section">${heNotice}${extractBody(htmlEn, 'ltr')}</div>
      ${divider}
      <div class="rtl-section" dir="rtl">${extractBody(htmlHe, 'rtl')}</div>
    </body></html>`;

    uiResults.push({
      ownerName:   ownerObj.name,
      ownerEmail,
      pdfUrl:      '',
      htmlContent: combinedHtml,
      warnings:    warnings.join(' '),
    });
  });

  return uiResults;
}

// ─────────────────────────────────────────────────────────────────────
// Entry point
// Auth: admin, owner, or maintenance_coordinator role.
// NOTE: Adding maintenance_coordinator here is only half the job.
// The role's table/page permissions in Base44 must also be restricted
// to the Generated Reports screen so Rodny cannot see other admin
// areas. Both steps are required — the code change alone is not enough.
// ─────────────────────────────────────────────────────────────────────

const ALLOWED_ROLES = ['admin', 'owner', 'maintenance_coordinator'];

Deno.serve(async (req) => {
  try {
    const base44 = createClientFromRequest(req);
    const user = await base44.auth.me();
    if (!user) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (!ALLOWED_ROLES.includes(user.role)) {
      return Response.json({ error: `Unauthorized: requires admin, owner, or maintenance_coordinator role (current: ${user.role})` }, { status: 403 });
    }

    const body = await req.json().catch(() => ({}));
    const reports = await doBuildReports(body?.payload || {}, base44);
    return Response.json({ reports, success: true });
  } catch (error) {
    console.error('buildInvestorReportsV9 error:', error);
    return Response.json({ error: error.message }, { status: 500 });
  }
});
