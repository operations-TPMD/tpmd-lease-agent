const express = require('express');
const path = require('path');
const { PORT } = require('./config');
const { handleFieldUpdate, handleNewOpportunity } = require('./handler');
const ghl = require('./ghl');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

app.get('/checklist', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'checklist.html'));
});

app.get('/api/checklist/:opportunityId', async (req, res) => {
  try {
    const opportunity = await ghl.getOpportunity(req.params.opportunityId);
    res.json({ opportunity });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/checklist/:opportunityId/update', async (req, res) => {
  const { fieldKey, value } = req.body;
  if (!fieldKey) return res.status(400).json({ error: 'fieldKey required' });

  try {
    const result = await handleFieldUpdate(req.params.opportunityId, fieldKey, value);
    res.json(result);
  } catch (err) {
    console.error('Update error:', err.response?.data || err.message);
    res.status(500).json({ error: err.message });
  }
});

app.post('/webhook/lease-renewal', async (req, res) => {
  const { type, opportunityId } = req.body;

  if (type === 'OpportunityCreate' && opportunityId) {
    handleNewOpportunity(opportunityId).catch(console.error);
  }

  res.status(200).json({ ok: true });
});

app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
