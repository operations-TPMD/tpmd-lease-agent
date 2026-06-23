const express = require('express');
const { PORT } = require('./config');
const { handleFieldUpdate } = require('./handler');

const app = express();
app.use(express.json());

app.post('/webhook/lease-renewal', async (req, res) => {
  const { type, contactId, opportunityId, customFields } = req.body;

  if (type !== 'ContactCustomFieldUpdated' && type !== 'OpportunityCustomFieldUpdated') {
    return res.status(200).json({ skip: true });
  }

  if (!opportunityId || !contactId || !Array.isArray(customFields)) {
    return res.status(400).json({ error: 'Missing required fields' });
  }

  try {
    const result = await handleFieldUpdate({ contactId, opportunityId, customFields });
    return res.status(200).json(result);
  } catch (err) {
    console.error('Webhook error:', err.response?.data || err.message);
    return res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, () => console.log(`Webhook listening on port ${PORT}`));
