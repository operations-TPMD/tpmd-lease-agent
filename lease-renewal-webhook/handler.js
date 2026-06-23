const { STAGE_IDS, PIPELINE_IDS, FIELD_KEYS } = require('./config');
const ghl = require('./ghl');

function getFieldValue(customFields, key) {
  const field = customFields.find(f => f.key === key);
  return field ? field.field_value : null;
}

function isTrue(val) {
  return val === true || val === 'true' || val === '1' || val === 'checked';
}

async function handleFieldUpdate(payload) {
  const { contactId, opportunityId, customFields: updatedFields } = payload;

  const opportunity = await ghl.getOpportunity(opportunityId);
  const currentStage = opportunity.pipelineStageId;

  const allFields = await ghl.getContactFields(contactId);
  const mergedFields = [...allFields];
  for (const uf of updatedFields) {
    const idx = mergedFields.findIndex(f => f.key === uf.key);
    if (idx >= 0) mergedFields[idx] = uf;
    else mergedFields.push(uf);
  }

  const get = (key) => getFieldValue(mergedFields, key);

  // Stage: Lease Expiring or Rent Decision Pending -> Renewal Offer Sent
  if (
    currentStage === STAGE_IDS.LEASE_EXPIRING ||
    currentStage === STAGE_IDS.RENT_DECISION_PENDING
  ) {
    if (isTrue(get(FIELD_KEYS.MANAGER_RENT_APPROVAL))) {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.RENEWAL_OFFER_SENT);
      return { moved: 'RENEWAL_OFFER_SENT' };
    }
  }

  // Stage: Renewal Offer Sent -> Inspection or Tenant Lost
  if (currentStage === STAGE_IDS.RENEWAL_OFFER_SENT) {
    const decision = get(FIELD_KEYS.TENANT_DECISION);

    if (decision === 'Renew') {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.INSPECTION);
      return { moved: 'INSPECTION' };
    }

    if (decision === 'Decline') {
      const forwardingAddress = get(FIELD_KEYS.FORWARDING_ADDRESS);
      if (!forwardingAddress) return { action: 'PROMPT_FORWARDING_ADDRESS' };

      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.TENANT_LOST, 'lost');
      await ghl.createMoveOutOpportunity(
        { id: contactId, name: opportunity.name },
        PIPELINE_IDS.MOVE_OUT,
        STAGE_IDS.MOVE_OUT_INITIAL
      );
      return { moved: 'TENANT_LOST', createdMoveOut: true };
    }
  }

  // Stage: Inspection -> Inspection Completed Pending Review
  if (currentStage === STAGE_IDS.INSPECTION) {
    if (isTrue(get(FIELD_KEYS.INSPECTION_COMPLETED))) {
      await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.INSPECTION_COMPLETED);
      return { moved: 'INSPECTION_COMPLETED' };
    }
  }

  // Stage: Inspection Completed Pending Review
  // Confirm Renewal: both checkboxes required -> Lease Renewed (won)
  // Reject Renewal: rejection letter + letter to owner -> Tenant Lost (lost) + Move Out
  if (currentStage === STAGE_IDS.INSPECTION_COMPLETED) {
    const managerDecision = get(FIELD_KEYS.MANAGER_FINAL_DECISION);

    if (managerDecision === 'Confirm Renewal') {
      if (
        isTrue(get(FIELD_KEYS.LEASE_SIGNED_BY_TENANT)) &&
        isTrue(get(FIELD_KEYS.LETTER_SENT_TO_OWNER))
      ) {
        await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.LEASE_RENEWED, 'won');
        return { moved: 'LEASE_RENEWED' };
      }
      return { action: 'WAITING_FOR_LEASE_SIGN_AND_LETTER' };
    }

    if (managerDecision === 'Reject Renewal') {
      if (
        isTrue(get(FIELD_KEYS.REJECTION_LETTER_SENT_TENANT)) &&
        isTrue(get(FIELD_KEYS.LETTER_SENT_TO_OWNER))
      ) {
        await ghl.updateOpportunityStage(opportunityId, STAGE_IDS.TENANT_LOST, 'lost');
        await ghl.createMoveOutOpportunity(
          { id: contactId, name: opportunity.name },
          PIPELINE_IDS.MOVE_OUT,
          STAGE_IDS.MOVE_OUT_INITIAL
        );
        return { moved: 'TENANT_LOST', createdMoveOut: true };
      }
      return { action: 'WAITING_FOR_REJECTION_LETTERS' };
    }
  }

  return { action: 'NO_TRANSITION' };
}

module.exports = { handleFieldUpdate };
