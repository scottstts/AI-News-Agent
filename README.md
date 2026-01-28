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

# GitHub Actions Deployment

The agent can run on a schedule via GitHub Actions. The workflow is defined in `.github/workflows/research.yml`.

## Required GitHub Secrets

Set the following secrets in your repository (Settings → Secrets and variables → Actions):

### API Keys (plain text)
- `GEMINI_API_KEY` - Google Gemini API key
- `OPENAI_API_KEY` - OpenAI API key
- `OPENROUTER_API_KEY` - OpenRouter API key
- `GCP_SERVICES_API_KEY` - GCP API key (for YouTube Data API)
- `GOOGLE_DRIVE_FOLDER_ID` - Target Google Drive folder ID
- `RECIPIENT_EMAIL` - Email address to receive research results

### Credential Files (base64 encoded)
These files need to be base64 encoded before adding as secrets:

```bash
# On macOS/Linux, encode each file:
base64 -i credentials/credentials.json | pbcopy  # copies to clipboard (macOS)
base64 credentials/credentials.json              # prints to stdout (Linux)
```

- `CREDENTIALS_JSON` - OAuth client credentials (`credentials/credentials.json`)
- `DRIVE_TOKEN_JSON` - Google Drive token (`credentials/drive_token.json`)
- `GMAIL_TOKEN_JSON` - Gmail token (`credentials/gmail_token.json`)

## Schedule

The workflow runs daily at 6:00 AM UTC by default. Edit the cron expression in `.github/workflows/research.yml` to change the schedule.

You can also trigger the workflow manually from the GitHub Actions tab using "Run workflow".