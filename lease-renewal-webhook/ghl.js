const axios = require('axios');
const { GHL_API_KEY, GHL_BASE_URL, GHL_LOCATION_ID } = require('./config');

const client = axios.create({
  baseURL: GHL_BASE_URL,
  headers: {
    Authorization: `Bearer ${GHL_API_KEY}`,
    Version: '2021-07-28',
    'Content-Type': 'application/json',
  },
});

async function getOpportunity(opportunityId) {
  const { data } = await client.get(`/opportunities/${opportunityId}`);
  return data.opportunity;
}

async function getOpportunityFields(opportunityId) {
  const opp = await getOpportunity(opportunityId);
  return opp.customFields || [];
}

async function updateOpportunityField(opportunityId, fieldKey, value) {
  const { data } = await client.put(`/opportunities/${opportunityId}`, {
    customFields: [{ key: fieldKey, field_value: value }],
  });
  return data;
}

async function updateOpportunityStage(opportunityId, stageId, status = null) {
  const payload = { stageId };
  if (status) payload.status = status;
  const { data } = await client.put(`/opportunities/${opportunityId}`, payload);
  return data;
}

async function createMoveOutOpportunity(contact, pipelineId, stageId) {
  const { data } = await client.post('/opportunities/', {
    pipelineId,
    locationId: GHL_LOCATION_ID,
    name: `Move Out - ${contact.name}`,
    pipelineStageId: stageId,
    status: 'open',
    contactId: contact.id,
  });
  return data;
}

module.exports = {
  getOpportunity,
  getOpportunityFields,
  updateOpportunityField,
  updateOpportunityStage,
  createMoveOutOpportunity,
};
