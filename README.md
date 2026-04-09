# The Property Management Doctor — Lease Agent

AI-powered lease lead management system for TPMD.

## Features

- Automated lead scanning every hour (9am-7pm ET)
- AI-powered decision making (send SMS, move stage, or skip)
- Web dashboard for reviewing and approving actions
- Instant webhook response to inbound messages
- DRY RUN and LIVE modes

## Deploy to Railway (Free)

1. **Create a GitHub repo**
   - Go to github.com, create new repo named `tpmd-lease-agent`
   - Don't initialize with README
   
2. **Push your code**
   ```bash
   cd "C:\Users\idanb\Desktop\Claude"
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/tpmd-lease-agent.git
   git push -u origin main
   ```

3. **Deploy on Railway**
   - Go to railway.app
   - Click "New Project" → "Deploy from GitHub"
   - Select your `tpmd-lease-agent` repo
   - Railway auto-detects Python and Procfile
   - Click "Deploy"
   - Wait 2-5 minutes, get your public URL

4. **Set Environment Variables on Railway**
   - In Railway dashboard, go to Variables
   - Add:
     - `GHL_API_KEY` = `pit-c65311e9-d2fb-49ff-8b63-6e0cb788e47c`
     - `GHL_LOCATION_ID` = `SjWHHlnUgHBOTcvFq7Vp`
     - `OPENAI_API_KEY` = your key
     - `ANTHROPIC_API_KEY` = your key

5. **Access**
   - Railway gives you a URL like `https://tpmd-lease-agent.railway.app`
   - Share with your team — no password needed

Done! Your dashboard is live.

## Local Development

```bash
python -m pip install -r requirements.txt
python dashboard.py
```

Open `http://localhost:8000`
