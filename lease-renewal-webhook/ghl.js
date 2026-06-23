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

async function updateOpportunityStage(opportunityId, stageId, status = null) {
  const payload = { stageId };
  if (status) payload.status = status;
  const { data } = await client.put(`/opportunities/${opportunityId}`, payload);
  return data;
}

async function updateContactField(contactId, fieldKey, value) {
  const { data } = await client.put(`/contacts/${contactId}`, {
    customFields: [{ key: fieldKey, field_value: value }],
  });
  return data;
}

async function getContactFields(contactId) {
  const { data } = await client.get(`/contacts/${contactId}`);
  return data.contact?.customFields || [];
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
  updateOpportunityStage,
  updateContactField,
  getContactFields,
  createMoveOutOpportunity,
};
