# AI News Agent

AI agent to get daily AI development news

`agent_core/` -- the agent logic

`services/` -- services needed

`app.py` -- run the app

# Setup

1. `python3 -m venv agent && source agent/bin/activate`

2. `pip install -r requirements.txt && playwright install --with-deps chromium`

3. Getting credentials and API keys:
    - AI API keys: Gemini, OpenAI, Anthropic, etc.
    - YouTube API key for YouTube video search (GCP enable YouTube API, create API key)
    - Credentials: enable Drive and Gmail API in GCP, create OAuth client, save client secret JSON as `credentials.json`
    - Run the agent and allow sign in the first time, creates the  `credentials/` dir and saves `drive_token.json` and `gmail_token.json` inside

4. Confirm: `.env` has all the API keys needed, `credentials/` has `credentials.json`, `drive_token.json`, and `gmail_token.json`

# Run Agent

`python3 app.py --now` run the agent right away only once

`python3 app.py` start the scheduler and run agent on schedule (running forever)