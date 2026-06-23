require('dotenv').config();

module.exports = {
  GHL_API_KEY: process.env.GHL_API_KEY,
  GHL_LOCATION_ID: process.env.GHL_LOCATION_ID,
  GHL_BASE_URL: 'https://services.leadconnectorhq.com',
  PORT: process.env.PORT || 3000,

  PIPELINE_IDS: {
    RENEWAL: process.env.RENEWAL_PIPELINE_ID,
    MOVE_OUT: process.env.MOVE_OUT_PIPELINE_ID,
  },

  STAGE_IDS: {
    LEASE_EXPIRING:           process.env.STAGE_LEASE_EXPIRING,
    RENT_DECISION_PENDING:    process.env.STAGE_RENT_DECISION_PENDING,
    RENEWAL_OFFER_SENT:       process.env.STAGE_RENEWAL_OFFER_SENT,
    INSPECTION:               process.env.STAGE_INSPECTION,
    INSPECTION_COMPLETED:     process.env.STAGE_INSPECTION_COMPLETED,
    LEASE_RENEWED:            process.env.STAGE_LEASE_RENEWED,
    TENANT_LOST:              process.env.STAGE_TENANT_LOST,
    MOVE_OUT_INITIAL:         process.env.STAGE_MOVE_OUT_INITIAL,
  },

  FIELD_KEYS: {
    MANAGER_RENT_APPROVAL:          'manager_rent_approval',
    TENANT_DECISION:                'tenant_decision',
    FORWARDING_ADDRESS:             'forwarding_address',
    INSPECTION_COMPLETED:           'inspection_completed',
    MANAGER_FINAL_DECISION:         'lease_approval',
    LEASE_SIGNED_BY_TENANT:         'lease_signed_by_tenant',
    LETTER_SENT_TO_OWNER:           'letter_sent_to_owner',
    REJECTION_LETTER_SENT_TENANT:   'rejection_letter_sent_to_tenant',
  },
};
