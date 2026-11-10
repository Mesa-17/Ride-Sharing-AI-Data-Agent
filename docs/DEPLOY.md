# Deploying the live demo

Three pieces: **Neon** hosts Postgres, **Railway** runs the API (`api/server.py`), and the portfolio
site (Netlify) serves the page. About $5/month in total (Railway Hobby); Neon's free tier is enough.

## 1. Database on Neon (free)

1. Sign up at neon.tech and create a project (pick a US East region).
2. Open **Connection details** and note the host, database (`neondb`), user (`neondb_owner`) and password.
3. In your local `.env`, point the loader at Neon:

   ```env
   DB_HOST=ep-xxxx.us-east-2.aws.neon.tech
   DB_PORT=5432
   DB_NAME=neondb
   DB_ADMIN_USER=neondb_owner
   DB_ADMIN_PASSWORD=<neon password>
   DB_AGENT_USER=agent_readonly
   DB_AGENT_PASSWORD=<make up a long random password>
   DB_SSLMODE=require
   ```

4. Load the data and create the read-only role:

   ```bash
   python feed_db.py
   ```

5. Check the role really is read-only:

   ```bash
   pytest tests/test_readonly_db.py
   ```

## 2. Anthropic key with a spend limit

1. In the Anthropic Console, create a new API key just for the demo.
2. Under **Limits**, set a monthly spend limit you're comfortable with (for example $10).
   The API also caps usage itself: 5 questions per visitor per hour and 300 per day for the whole site.

## 3. API on Railway (about $5/month)

1. Push this repo to GitHub.
2. At railway.com: **New project → Deploy from GitHub repo** → pick this repo. Railway finds the `Dockerfile`.
3. Add these variables in the service's **Variables** tab. The server only needs the read-only user, so
   the admin password never goes on the server.

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | the demo key |
   | `DB_HOST`, `DB_PORT`, `DB_NAME` | from Neon |
   | `DB_AGENT_USER` | `agent_readonly` |
   | `DB_AGENT_PASSWORD` | the password you chose above |
   | `DB_SSLMODE` | `require` |
   | `ALLOWED_ORIGINS` | `https://sandeepmenon.dev` |
   | `DEMO_PER_HOUR` / `DEMO_PER_DAY` | `5` / `300` (optional) |

4. **Settings → Networking → Generate domain.** You get a URL like `https://ride-agent-production.up.railway.app`.
5. Open `<that URL>/health`. It should say `{"ok":true}`.
6. Logs: each question is logged as one JSON line (question, status, time, a hashed visitor id; never the raw IP).

## 4. Record real runs for the example questions

The page replays these when the API is down or a visitor hits the limit, so the examples always show a real run.

```bash
python scripts/record_demo.py        # runs every example in web/examples.json, writes web/recorded.json
```

## 5. Connect the page

Give the portfolio page the API URL from step 3 and copy `web/recorded.json` into the site.

## Test it all locally first

```bash
uvicorn api.server:app --reload                 # API on http://localhost:8000
python -m http.server 4321 --directory web      # open http://localhost:4321
```

On localhost the page talks to the local API automatically.
