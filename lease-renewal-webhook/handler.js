const { STAGE_IDS, PIPELINE_IDS, FIELD_KEYS } = require('./config');
const ghl = require('./ghl');
const emails = require('./emails');

function fieldVal(fields, key) {
  const f = fields.find(f => f.key === key);
  return f ? f.field_value : null;
}

function isTrue(val) {
  return val === true || val === 'true' || val === '1' || val === 'checked';
}

function checklistUrl(opportunityId) {
  return `${process.env.SERVER_URL}/checklist?id=${opportunityId}`;
}

async function handleFieldUpdate(opportunityId, fieldKey, value) {
  await ghl.updateOpportunityField(opportunityId, fieldKey, value);

  const opportunity = await ghl.getOpportunity(opportunityId);
  const currentStage = opportunity.pipelineStageId;
  const fields = opportunity.customFields || [];
  const name = opportunity.name;
  const url = checklistUrl(opportunityId);

  const get = (key) => fieldVal(fields, key);

  if (
    currentStage === STAGE_IDS.LEASE_EXPIRING ||
    currentStage === STAGE_IDS.RENT_DECISION_PENDING
  ) {
    if (isTrue(get(FIELD_KEYS.MANAGER_RENT_APPROVAL))) {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.RENEWAL_OFFER_SENT);
      await emails.notifyRentApproved(name, url);
      return { moved: 'RENEWAL_OFFER_SENT' };
    }
  }

  if (currentStage === STAGE_IDS.RENEWAL_OFFER_SENT) {
    const decision = get(FIELD_KEYS.TENANT_DECISION);

    if (decision === 'Renew') {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.INSPECTION);
      await emails.notifyTenantRenews(name, url);
      return { moved: 'INSPECTION' };
    }

    if (decision === 'Decline') {
      const address = get(FIELD_KEYS.FORWARDING_ADDRESS);
      if (!address) return { action: 'PROMPT_FORWARDING_ADDRESS' };

      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.TENANT_LOST, 'lost');
      await ghl.createMoveOutOpportunity(
        { id: opportunity.contactId, name },
        PIPELINE_IDS.MOVE_OUT,
        STAGE_IDS.MOVE_OUT_INITIAL
      );
      await emails.notifyTenantDeclines(name, url);
      return { moved: 'TENANT_LOST', createdMoveOut: true };
    }
  }

  if (currentStage === STAGE_IDS.INSPECTION) {
    if (isTrue(get(FIELD_KEYS.INSPECTION_COMPLETED))) {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.INSPECTION_COMPLETED);
      await emails.notifyInspectionComplete(name, url);
      return { moved: 'INSPECTION_COMPLETED' };
    }
  }

  if (currentStage === STAGE_IDS.INSPECTION_COMPLETED) {
    const decision = get(FIELD_KEYS.MANAGER_FINAL_DECISION);

    if (decision === 'Confirm Renewal') {
      if (!emails._confirmSent) await emails.notifyConfirmRenewal(name, url);

      if (isTrue(get(FIELD_KEYS.LEASE_SIGNED_BY_TENANT)) && isTrue(get(FIELD_KEYS.LETTER_SENT_TO_OWNER))) {
        await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.LEASE_RENEWED, 'won');
        await emails.notifyLeaseRenewed(name, url);
        return { moved: 'LEASE_RENEWED' };
      }
      return { action: 'WAITING_FOR_LEASE_SIGN_AND_LETTER' };
    }

    if (decision === 'Reject Renewal') {
      if (!emails._rejectSent) await emails.notifyRejected(name, url);

      if (isTrue(get(FIELD_KEYS.REJECTION_LETTER_SENT_TENANT)) && isTrue(get(FIELD_KEYS.LETTER_SENT_TO_OWNER))) {
        await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.TENANT_LOST, 'lost');
        await ghl.createMoveOutOpportunity(
          { id: opportunity.contactId, name },
          PIPELINE_IDS.MOVE_OUT,
          STAGE_IDS.MOVE_OUT_INITIAL
        );
        await emails.notifyTenantLost(name, url);
        return { moved: 'TENANT_LOST', createdMoveOut: true };
      }
      return { action: 'WAITING_FOR_REJECTION_LETTERS' };
    }
  }

  return { action: 'NO_TRANSITION' };
}

async function handleNewOpportunity(opportunityId) {
  const opportunity = await ghl.getOpportunity(opportunityId);
  if (opportunity.pipelineId !== PIPELINE_IDS.RENEWAL) return;
  await emails.notifyNewLead(opportunity.name, checklistUrl(opportunityId));
}

module.exports = { handleFieldUpdate, handleNewOpportunity };
