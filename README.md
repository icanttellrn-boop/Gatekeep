# KeyBot

Discord key bot + HTTP loader API in one file (`bot.py`).

## 1. Create the Discord bot
1. https://discord.com/developers/applications -> New Application -> **Bot** -> Reset Token -> copy it.
2. No privileged intents needed.
3. OAuth2 -> URL Generator: scopes `bot` + `applications.commands`; permissions: Send Messages, Embed Links (+ Manage Roles if using BUYER_ROLE_ID). Open the URL to invite it.

## 2. Run it
```
pip install -r requirements.txt
cp .env.example .env     # fill in DISCORD_TOKEN (BASE_URL only needed off Railway)
python bot.py
```

## 3. Use it (in Discord)
1. `/addscript name:<anything> file:<your .lua>`  -> returns a script ID
2. `/genkey script:<pick> days:30 amount:5`     -> keys (days 0 = lifetime)
3. `/panel` in your #lua-free channel            -> posts the Redeem / Get Script / Reset HWID / My Stats buttons
Other admin commands: `/grant`, `/keyinfo`, `/blacklist`, `/forcereset`, `/updatescript`, `/scripts`.

Users: Redeem -> Get Script -> paste the 2-line loader into their executor.

## Hosting on Railway
1. Push this folder to a GitHub repo (`.env` is git-ignored, don't commit it).
2. railway.com -> **New Project -> Deploy from GitHub repo** -> pick the repo. It detects Python from `requirements.txt`; `railway.json` sets the start command and health check.
3. **Variables** tab: add `DISCORD_TOKEN` (plus any optional ones from `.env.example`).
4. **Settings -> Networking -> Generate Domain**. `BASE_URL` is picked up automatically from `RAILWAY_PUBLIC_DOMAIN` (only set `BASE_URL` yourself for a custom domain).
5. **Add a Volume** (right-click the service canvas -> Volume, or Ctrl/Cmd+K -> "volume") and mount it at `/data`. The bot stores `keybot.db` there automatically, so keys and scripts survive redeploys. **Without a volume the database is wiped on every deploy.**
6. Check **Deployments -> Logs** for `Logged in as ...` and `HTTP API listening`.

Test: open `https://YOUR-APP.up.railway.app/` -> should say `ok`.

Railway's free trial/credits are limited; a small always-on bot is roughly a few dollars/month on the Hobby plan.

## Security notes
- Keep `.env` and your bot token private; never commit them.
- Key + HWID only controls who is *served* the script. Anything a client runs can be dumped by that client, so don't put secrets in the script itself.
