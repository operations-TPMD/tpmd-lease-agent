const axios = require('axios');

const MAKE_HOOK = process.env.MAKE_MAILHOOK_ADDRESS;

const TR = 'tr@tpmd.io';
const WO = 'wo@tpmd.io';
const MGR = 'manager@tpmd.io';

async function send(to, subject, body) {
  await axios.post(MAKE_HOOK, {
    to: Array.isArray(to) ? to.join(',') : to,
    subject,
    body,
  });
}

function checklistBtn(url) {
  return url ? `\n\nOpen Checklist: ${url}` : '';
}

async function notifyNewLead(name, url) {
  await send(TR, `New Renewal Lead: ${name}`,
    `A new lease renewal has been added to the pipeline.\n\nProperty: ${name}\n\nPlease begin the renewal process.${checklistBtn(url)}`);
}

async function notifyRentApproved(name, url) {
  await send(MGR, `Rent Increase Approved — ${name}`,
    `The rent increase has been approved for ${name}.\n\nThe renewal offer is ready to be sent to the tenant.${checklistBtn(url)}`);
}

async function notifyTenantRenews(name, url) {
  await send(WO, `Inspection Needed — ${name}`,
    `Tenant at ${name} confirmed renewal.\n\nPlease schedule and complete the property inspection.${checklistBtn(url)}`);
}

async function notifyTenantDeclines(name, url) {
  await send([MGR, TR], `Tenant Declined Renewal — ${name}`,
    `Tenant at ${name} has declined the renewal offer.\n\nThe lead will be moved to Tenant Lost and a Move Out will be created.${checklistBtn(url)}`);
}

async function notifyInspectionComplete(name, url) {
  await send(MGR, `Inspection Completed — ${name}`,
    `Inspection at ${name} has been completed and photos uploaded.\n\nPlease review and make a final decision.${checklistBtn(url)}`);
}

async function notifyConfirmRenewal(name, url) {
  await send(TR, `Renewal Confirmed — ${name}`,
    `Manager confirmed the renewal for ${name}.\n\nPlease follow up with the tenant to sign the lease and send the owner letter.${checklistBtn(url)}`);
}

async function notifyLeaseRenewed(name, url) {
  await send(MGR, `Lease Renewed — ${name}`,
    `Lease at ${name} has been signed by the tenant and the owner letter has been sent.\n\nThis renewal is now complete.${checklistBtn(url)}`);
}

async function notifyRejected(name, url) {
  await send([MGR, TR], `Renewal Rejected — ${name}`,
    `Manager rejected the renewal for ${name}.\n\nPlease send formal letters to the tenant and owner, then mark them as sent.${checklistBtn(url)}`);
}

async function notifyTenantLost(name, url) {
  await send(MGR, `Tenant Lost — ${name}`,
    `All rejection letters sent for ${name}.\n\nThe lead has been moved to Tenant Lost and a Move Out has been created.${checklistBtn(url)}`);
}

module.exports = {
  notifyNewLead,
  notifyRentApproved,
  notifyTenantRenews,
  notifyTenantDeclines,
  notifyInspectionComplete,
  notifyConfirmRenewal,
  notifyLeaseRenewed,
  notifyRejected,
  notifyTenantLost,
};
