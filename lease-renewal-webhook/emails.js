const nodemailer = require('nodemailer');

const transporter = nodemailer.createTransport({
  service: 'gmail',
  auth: {
    user: process.env.GMAIL_USER,
    pass: process.env.GMAIL_APP_PASSWORD,
  },
});

const TR = 'tr@tpmd.io';
const WO = 'wo@tpmd.io';
const MGR = 'manager@tpmd.io';

function wrap(title, body) {
  return `
    <div style="font-family:Poppins,Arial,sans-serif;max-width:520px;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
      <div style="background:linear-gradient(135deg,#d946ef,#7c3aed);padding:24px;color:#fff;">
        <div style="font-size:11px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;opacity:0.7;margin-bottom:4px;">The Property Management Doctor</div>
        <h2 style="margin:0;font-size:20px;font-weight:700;">${title}</h2>
      </div>
      <div style="padding:24px;">${body}</div>
    </div>`;
}

function checklistBtn(url) {
  return `<a href="${url}" style="display:inline-block;margin-top:16px;background:linear-gradient(135deg,#d946ef,#7c3aed);color:#fff;text-decoration:none;padding:10px 22px;border-radius:8px;font-weight:700;font-size:14px;">Open Checklist</a>`;
}

function row(label, value) {
  return `<div style="background:#f8fafc;border-radius:8px;padding:14px;border-left:3px solid #a855f7;margin-bottom:12px;">
    <div style="font-size:12px;color:#64748b;text-transform:uppercase;font-weight:700;letter-spacing:0.05em;">${label}</div>
    <div style="color:#0f172a;font-weight:600;margin-top:2px;">${value}</div>
  </div>`;
}

async function send(to, subject, html) {
  await transporter.sendMail({
    from: `"TPMD Renewals" <${process.env.GMAIL_USER}>`,
    to: Array.isArray(to) ? to.join(',') : to,
    subject,
    html,
  });
}

async function notifyNewLead(opportunityName, checklistUrl) {
  await send(TR, `New Renewal Lead: ${opportunityName}`, wrap(
    'New Renewal Lead',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">A new lease renewal has been added to the pipeline. Please begin the renewal process.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyRentApproved(opportunityName, checklistUrl) {
  await send(MGR, `Rent Increase Approved — ${opportunityName}`, wrap(
    'Rent Increase Approved',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">The rent increase has been approved. The renewal offer is ready to be sent to the tenant.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyTenantRenews(opportunityName, checklistUrl) {
  await send(WO, `Inspection Needed — ${opportunityName}`, wrap(
    'Tenant Wants to Renew',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">Tenant confirmed renewal. Please schedule and complete the property inspection.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyTenantDeclines(opportunityName, checklistUrl) {
  await send([MGR, TR], `Tenant Declined Renewal — ${opportunityName}`, wrap(
    'Tenant Declined Renewal',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">Tenant has declined the renewal offer. The lead will be moved to Tenant Lost and a Move Out will be created.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyInspectionComplete(opportunityName, checklistUrl) {
  await send(MGR, `Inspection Completed — ${opportunityName}`, wrap(
    'Inspection Completed',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">The inspection has been completed and photos uploaded. Please review and make a final decision.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyConfirmRenewal(opportunityName, checklistUrl) {
  await send(TR, `Renewal Confirmed — ${opportunityName}`, wrap(
    'Renewal Confirmed',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">Manager confirmed the renewal. Please follow up with the tenant to sign the lease and send the owner letter.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyLeaseRenewed(opportunityName, checklistUrl) {
  await send(MGR, `Lease Renewed — ${opportunityName}`, wrap(
    'Lease Renewed',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">Lease has been signed by the tenant and the owner letter has been sent. This renewal is now complete.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyRejected(opportunityName, checklistUrl) {
  await send([MGR, TR], `Renewal Rejected — ${opportunityName}`, wrap(
    'Renewal Rejected',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">Manager has rejected the renewal. Please send formal letters to the tenant and owner, then mark them as sent.</p>
     ${checklistBtn(checklistUrl)}`
  ));
}

async function notifyTenantLost(opportunityName, checklistUrl) {
  await send(MGR, `Tenant Lost — ${opportunityName}`, wrap(
    'Tenant Lost',
    `${row('Property', opportunityName)}
     <p style="color:#475569;margin:0;">All rejection letters have been sent. This lead has been moved to Tenant Lost and a Move Out has been created.</p>
     ${checklistBtn(checklistUrl)}`
  ));
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
