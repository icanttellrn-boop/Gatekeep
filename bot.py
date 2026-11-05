"""
KeyBot - Discord key bot + HTTP loader/validation API in ONE file.

Discord : panel buttons (Redeem / Get Script / Reset HWID / My Stats)
          + admin slash commands (/addscript /genkey /grant /panel ...)
HTTP    : GET /v/<script_id>          Lua loader stub (what your users run)
          GET /api/load/<script_id>   checks key + HWID, returns the script
          GET /api/check/<script_id>  same check, JSON only (for custom key UIs)
"""
import collections
import json
import logging
import os
import secrets
import sqlite3
import time

import discord
from aiohttp import web
from discord import app_commands

try:  # optional: load a local .env file
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------- config ---
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.getenv("GUILD_ID") or 0) or None          # set = commands appear instantly
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BUYER_ROLE_ID = int(os.getenv("BUYER_ROLE_ID") or 0) or None  # role given on redeem
KEY_PREFIX = os.getenv("KEY_PREFIX", "KEY").upper()
BRAND = os.getenv("BRAND", "KeyBot")
RESET_COOLDOWN_H = int(os.getenv("HWID_RESET_COOLDOWN_HOURS", "24"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "40"))
PORT = int(os.getenv("PORT", "8080"))
_rw_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")  # set by Railway once you generate a domain
BASE_URL = (os.getenv("BASE_URL") or (f"https://{_rw_domain}" if _rw_domain else "") or f"http://localhost:{PORT}").rstrip("/")
_rw_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")  # set by Railway when a Volume is attached
DB_PATH = os.getenv("DB_PATH") or (os.path.join(_rw_volume, "keybot.db") if _rw_volume else "keybot.db")
COLOUR = 0xDC143C

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("keybot")

# --------------------------------------------------------------- database ---
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS scripts(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY, script_id TEXT NOT NULL, days INTEGER, created_at INTEGER NOT NULL,
  owner INTEGER, redeemed_at INTEGER, expires_at INTEGER,
  hwid TEXT, resets INTEGER NOT NULL DEFAULT 0, last_reset INTEGER,
  banned INTEGER NOT NULL DEFAULT 0, execs INTEGER NOT NULL DEFAULT 0, last_exec INTEGER);
CREATE INDEX IF NOT EXISTS keys_owner ON keys(owner);
"""
)
db.commit()


def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    db.commit()
    return cur.fetchone() if one else cur.fetchall()


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


def new_key():
    groups = ["".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4)]
    return f"{KEY_PREFIX}-" + "-".join(groups)


def norm(key):
    return key.strip().upper()


def key_state(k, now=None):
    now = now or int(time.time())
    if k["banned"]:
        return "BANNED"
    if k["owner"] is None:
        return "UNREDEEMED"
    if k["expires_at"] and k["expires_at"] < now:
        return "EXPIRED"
    return "ACTIVE"


def fmt_exp(ts):
    return "`Lifetime`" if not ts else f"<t:{ts}:R>"


def redeem(key, user_id):
    """Attach an unredeemed key to a Discord user. Returns (row, error)."""
    k = q("SELECT * FROM keys WHERE key=?", (key,), one=True)
    if not k:
        return None, "That key doesn't exist."
    if k["banned"]:
        return None, "That key has been revoked."
    if k["owner"] is not None:
        return None, "That key was already redeemed."
    now = int(time.time())
    exp = now + k["days"] * 86400 if k["days"] else None
    q("UPDATE keys SET owner=?, redeemed_at=?, expires_at=? WHERE key=?", (user_id, now, exp, key))
    return q("SELECT * FROM keys WHERE key=?", (key,), one=True), None


def user_keys(user_id, active_only=False, limit=10):
    now = int(time.time())
    sql = (
        "SELECT keys.*, scripts.name AS name FROM keys JOIN scripts ON scripts.id=keys.script_id "
        "WHERE owner=?"
    )
    args = [user_id]
    if active_only:
        sql += " AND banned=0 AND (expires_at IS NULL OR expires_at > ?)"
        args.append(now)
    sql += " ORDER BY redeemed_at DESC LIMIT ?"
    args.append(limit)
    return q(sql, args)


def validate(script_id, key, hwid, count=True):
    """Core check used by the HTTP API. Returns (code, row)."""
    key = norm(key)
    if not key or len(key) > 64:
        return "KEY_INVALID", None
    k = q("SELECT * FROM keys WHERE key=?", (key,), one=True)
    if not k or k["script_id"] != script_id:
        return "KEY_INVALID", None
    if k["banned"]:
        return "KEY_BLACKLISTED", k
    if k["owner"] is None:
        return "KEY_NOT_REDEEMED", k
    now = int(time.time())
    if k["expires_at"] and k["expires_at"] < now:
        return "KEY_EXPIRED", k
    if not hwid or len(hwid) > 128:
        return "HWID_MISSING", k
    if k["hwid"] is None:  # first use locks the key to this device
        q("UPDATE keys SET hwid=? WHERE key=?", (hwid, key))
    elif k["hwid"] != hwid:
        return "KEY_HWID_LOCKED", k
    if count:
        q("UPDATE keys SET execs=execs+1, last_exec=? WHERE key=?", (now, key))
    return "KEY_VALID", k


MESSAGES = {
    "KEY_INVALID": "Invalid key.",
    "KEY_NOT_REDEEMED": "Redeem this key in our Discord first.",
    "KEY_EXPIRED": "Your key has expired.",
    "KEY_BLACKLISTED": "This key is blacklisted.",
    "KEY_HWID_LOCKED": "Key is locked to another device. Reset your HWID in Discord.",
    "HWID_MISSING": "Could not read your HWID.",
}

# -------------------------------------------------------------- HTTP API ---
LOADER = r'''local BASE, SID = "__BASE__", "__SID__"
local key = _G.ScriptKey or script_key
if type(key) ~= "string" or key == "" then
    return warn('[Loader] Set _G.ScriptKey = "YOUR-KEY" before running the loader.')
end
local hwid
pcall(function()
    hwid = (gethwid and gethwid()) or game:GetService("RbxAnalyticsService"):GetClientId()
end)
if not hwid then return warn("[Loader] Could not read your HWID.") end
local HS = game:GetService("HttpService")
local url = BASE .. "/api/load/" .. SID .. "?key=" .. HS:UrlEncode(key) .. "&hwid=" .. HS:UrlEncode(tostring(hwid))
local ok, body = pcall(function() return game:HttpGet(url) end)
if not ok then return warn("[Loader] Server unreachable.") end
local fn, err = loadstring(body)
if not fn then return warn("[Loader] " .. tostring(err)) end
return fn()
'''


def lua_warn(text):
    return f"warn({json.dumps('[Loader] ' + text)})"


hits = collections.defaultdict(collections.deque)


@web.middleware
async def ratelimit(req, handler):
    if req.path == "/":  # health check / uptime pings are free
        return await handler(req)
    ip = req.headers.get("X-Forwarded-For", req.remote or "?").split(",")[0].strip()
    now = time.monotonic()
    dq = hits[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return web.Response(status=429, text=lua_warn("Rate limited, slow down."))
    dq.append(now)
    if len(hits) > 5000:
        hits.clear()
    return await handler(req)


async def h_health(_):
    return web.Response(text="ok")


async def h_loader(req):
    sid = req.match_info["script_id"]
    if not q("SELECT 1 FROM scripts WHERE id=?", (sid,), one=True):
        return web.Response(text=lua_warn("Unknown script."), content_type="text/plain")
    body = LOADER.replace("__BASE__", BASE_URL).replace("__SID__", sid)
    return web.Response(text=body, content_type="text/plain")


async def h_load(req):
    sid = req.match_info["script_id"]
    code, _ = validate(sid, req.query.get("key", ""), req.query.get("hwid", ""))
    if code != "KEY_VALID":
        return web.Response(text=lua_warn(MESSAGES[code]), content_type="text/plain")
    s = q("SELECT source FROM scripts WHERE id=?", (sid,), one=True)
    return web.Response(text=s["source"], content_type="text/plain")


async def h_check(req):
    sid = req.match_info["script_id"]
    code, k = validate(sid, req.query.get("key", ""), req.query.get("hwid", ""), count=False)
    exp = k["expires_at"] if k and code == "KEY_VALID" else None
    return web.json_response({"code": code, "message": MESSAGES.get(code, "OK"), "expires_at": exp})


def make_web_app():
    app = web.Application(middlewares=[ratelimit])
    app.add_routes(
        [
            web.get("/", h_health),
            web.get("/v/{script_id}", h_loader),
            web.get("/api/load/{script_id}", h_load),
            web.get("/api/check/{script_id}", h_check),
        ]
    )
    return app


# ------------------------------------------------------------ Discord UI ---
def loader_code(key, sid):
    return f'_G.ScriptKey = "{key}"\nloadstring(game:HttpGet("{BASE_URL}/v/{sid}"))()'


def script_embed(r):
    e = discord.Embed(title=f"📜 {r['name']}", colour=COLOUR)
    e.add_field(name="Loader (copy & run)", value=f"```lua\n{loader_code(r['key'], r['script_id'])}\n```", inline=False)
    e.add_field(name="Key expires", value=fmt_exp(r["expires_at"]))
    return e


async def give_role(member):
    if not BUYER_ROLE_ID or not isinstance(member, discord.Member):
        return
    role = member.guild.get_role(BUYER_ROLE_ID)
    if role:
        try:
            await member.add_roles(role, reason="Key redeemed")
        except discord.HTTPException:
            log.warning("Could not add buyer role (check Manage Roles + role order)")


class RedeemModal(discord.ui.Modal, title="Redeem a key"):
    key_input = discord.ui.TextInput(
        label="Your key", placeholder=f"{KEY_PREFIX}-XXXX-XXXX-XXXX-XXXX", min_length=8, max_length=64
    )

    async def on_submit(self, i: discord.Interaction):
        k, err = redeem(norm(self.key_input.value), i.user.id)
        if err:
            return await i.response.send_message(f"❌ {err}", ephemeral=True)
        await give_role(i.user)
        name = q("SELECT name FROM scripts WHERE id=?", (k["script_id"],), one=True)["name"]
        await i.response.send_message(
            f"✅ Redeemed **{name}**! Expires: {fmt_exp(k['expires_at'])}\nPress **Get Script** to grab your loader.",
            ephemeral=True,
        )


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent: survives restarts

    @discord.ui.button(label="Redeem", style=discord.ButtonStyle.success, emoji="🎟️", custom_id="kb:redeem")
    async def b_redeem(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(RedeemModal())

    @discord.ui.button(label="Get Script", style=discord.ButtonStyle.primary, emoji="📜", custom_id="kb:script")
    async def b_script(self, i: discord.Interaction, b: discord.ui.Button):
        rows = user_keys(i.user.id, active_only=True, limit=5)
        if not rows:
            return await i.response.send_message("You have no active key. Press **Redeem** first.", ephemeral=True)
        await i.response.send_message(embeds=[script_embed(r) for r in rows], ephemeral=True)

    @discord.ui.button(label="Reset HWID", style=discord.ButtonStyle.danger, emoji="🔄", custom_id="kb:reset")
    async def b_reset(self, i: discord.Interaction, b: discord.ui.Button):
        rows = user_keys(i.user.id, active_only=True)
        if not rows:
            return await i.response.send_message("You have no active key.", ephemeral=True)
        now = int(time.time())
        lines = []
        for r in rows:
            wait = (r["last_reset"] or 0) + RESET_COOLDOWN_H * 3600 - now
            if r["hwid"] is None:
                lines.append(f"• **{r['name']}**: no device linked yet")
            elif wait > 0:
                lines.append(f"• **{r['name']}**: on cooldown, try again <t:{now + wait}:R>")
            else:
                q("UPDATE keys SET hwid=NULL, resets=resets+1, last_reset=? WHERE key=?", (now, r["key"]))
                lines.append(f"• **{r['name']}**: ✅ HWID reset")
        await i.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="My Stats", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="kb:stats")
    async def b_stats(self, i: discord.Interaction, b: discord.ui.Button):
        rows = user_keys(i.user.id)
        if not rows:
            return await i.response.send_message("You haven't redeemed a key yet.", ephemeral=True)
        e = discord.Embed(title="📊 Your stats", colour=COLOUR)
        for r in rows:
            last = f"<t:{r['last_exec']}:R>" if r["last_exec"] else "never"
            e.add_field(
                name=r["name"],
                value=(
                    f"Status: **{key_state(r)}**\nExpires: {fmt_exp(r['expires_at'])}\n"
                    f"Device linked: {'yes' if r['hwid'] else 'no'}\nExecutions: {r['execs']} (last {last})\n"
                    f"HWID resets: {r['resets']}"
                ),
                inline=True,
            )
        await i.response.send_message(embed=e, ephemeral=True)


# ------------------------------------------------------------------- bot ---
class KeyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())  # no privileged intents needed
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PanelView())
        runner = web.AppRunner(make_web_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("HTTP API listening on :%s (public URL %s)", PORT, BASE_URL)
        if GUILD_ID:
            g = discord.Object(GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)


bot = KeyBot()


def admin_only():
    def pred(i: discord.Interaction):
        perms = getattr(i.user, "guild_permissions", None)
        return i.user.id in ADMIN_IDS or bool(perms and perms.manage_guild)

    return app_commands.check(pred)


@bot.tree.error
async def on_command_error(i: discord.Interaction, e: app_commands.AppCommandError):
    denied = isinstance(e, app_commands.CheckFailure)
    if not denied:
        log.exception("command error", exc_info=e)
    msg = "Admins only." if denied else "Something went wrong."
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


async def script_ac(i: discord.Interaction, current: str):
    rows = q("SELECT id, name FROM scripts ORDER BY name")
    return [app_commands.Choice(name=r["name"], value=r["id"]) for r in rows if current.lower() in r["name"].lower()][:25]


# ---- admin commands ----
@bot.tree.command(description="Post the key panel in this channel")
@app_commands.guild_only()
@admin_only()
async def panel(
    i: discord.Interaction,
    title: str = "Get your script",
    description: str = "Redeem your key, grab your loader, reset your HWID, or check your stats.",
):
    e = discord.Embed(title=title, description=description, colour=COLOUR)
    e.set_footer(text=BRAND)
    await i.channel.send(embed=e, view=PanelView())
    await i.response.send_message("Panel posted.", ephemeral=True)


@bot.tree.command(description="Upload a new script (.lua or .txt)")
@app_commands.guild_only()
@admin_only()
async def addscript(i: discord.Interaction, name: str, file: discord.Attachment):
    if file.size > 2_000_000:
        return await i.response.send_message("File too big (2 MB max).", ephemeral=True)
    await i.response.defer(ephemeral=True)
    src = (await file.read()).decode("utf-8", "replace")
    sid = secrets.token_hex(8)
    q("INSERT INTO scripts VALUES(?,?,?,?)", (sid, name, src, int(time.time())))
    await i.followup.send(f"✅ **{name}** added.\nScript ID: `{sid}`\nNow run `/genkey`.", ephemeral=True)


@bot.tree.command(description="Replace a script's code (users get it on next run)")
@app_commands.guild_only()
@admin_only()
@app_commands.autocomplete(script=script_ac)
async def updatescript(i: discord.Interaction, script: str, file: discord.Attachment):
    if not q("SELECT 1 FROM scripts WHERE id=?", (script,), one=True):
        return await i.response.send_message("Unknown script.", ephemeral=True)
    if file.size > 2_000_000:
        return await i.response.send_message("File too big (2 MB max).", ephemeral=True)
    await i.response.defer(ephemeral=True)
    q("UPDATE scripts SET source=? WHERE id=?", ((await file.read()).decode("utf-8", "replace"), script))
    await i.followup.send("✅ Script updated.", ephemeral=True)


@bot.tree.command(description="List scripts")
@app_commands.guild_only()
@admin_only()
async def scripts(i: discord.Interaction):
    rows = q(
        "SELECT scripts.id, scripts.name, "
        "(SELECT COUNT(*) FROM keys WHERE script_id=scripts.id) AS n FROM scripts"
    )
    text = "\n".join(f"**{r['name']}** | `{r['id']}` | {r['n']} keys" for r in rows) or "No scripts yet. Use `/addscript`."
    await i.response.send_message(text, ephemeral=True)


@bot.tree.command(description="Generate keys (days 0 = lifetime)")
@app_commands.guild_only()
@admin_only()
@app_commands.autocomplete(script=script_ac)
@app_commands.describe(days="Valid days after redeem, 0 = lifetime", amount="How many keys")
async def genkey(
    i: discord.Interaction,
    script: str,
    days: app_commands.Range[int, 0, 3650] = 0,
    amount: app_commands.Range[int, 1, 50] = 1,
):
    if not q("SELECT 1 FROM scripts WHERE id=?", (script,), one=True):
        return await i.response.send_message("Unknown script.", ephemeral=True)
    keys = []
    for _ in range(amount):
        k = new_key()
        q("INSERT INTO keys(key, script_id, days, created_at) VALUES(?,?,?,?)", (k, script, days or None, int(time.time())))
        keys.append(k)
    await i.response.send_message("```\n" + "\n".join(keys) + "\n```", ephemeral=True)


@bot.tree.command(description="Give a user a key directly (days 0 = lifetime)")
@app_commands.guild_only()
@admin_only()
@app_commands.autocomplete(script=script_ac)
async def grant(i: discord.Interaction, user: discord.Member, script: str, days: app_commands.Range[int, 0, 3650] = 0):
    if not q("SELECT 1 FROM scripts WHERE id=?", (script,), one=True):
        return await i.response.send_message("Unknown script.", ephemeral=True)
    now = int(time.time())
    q(
        "INSERT INTO keys(key, script_id, days, created_at, owner, redeemed_at, expires_at) VALUES(?,?,?,?,?,?,?)",
        (new_key(), script, days or None, now, user.id, now, now + days * 86400 if days else None),
    )
    await give_role(user)
    await i.response.send_message(f"✅ Granted to {user.mention}. They can press **Get Script** on the panel.", ephemeral=True)


@bot.tree.command(description="Look up a key")
@app_commands.guild_only()
@admin_only()
async def keyinfo(i: discord.Interaction, key: str):
    k = q(
        "SELECT keys.*, scripts.name AS name FROM keys JOIN scripts ON scripts.id=keys.script_id WHERE key=?",
        (norm(key),),
        one=True,
    )
    if not k:
        return await i.response.send_message("Key not found.", ephemeral=True)
    exp = f"{k['days']} days after redeem" if k["owner"] is None and k["days"] else fmt_exp(k["expires_at"])
    last = f"<t:{k['last_exec']}:R>" if k["last_exec"] else "never"
    owner = f"<@{k['owner']}>" if k["owner"] else "nobody"
    await i.response.send_message(
        f"Script: **{k['name']}**\nStatus: **{key_state(k)}**\nOwner: {owner}\n"
        f"Expires: {exp}\nDevice linked: {'yes' if k['hwid'] else 'no'}\n"
        f"Executions: {k['execs']} (last {last})\nHWID resets: {k['resets']}",
        ephemeral=True,
    )


@bot.tree.command(description="Blacklist or un-blacklist a key")
@app_commands.guild_only()
@admin_only()
async def blacklist(i: discord.Interaction, key: str, banned: bool = True):
    q("UPDATE keys SET banned=? WHERE key=?", (int(banned), norm(key)))
    await i.response.send_message("🚫 Key blacklisted." if banned else "✅ Key restored.", ephemeral=True)


@bot.tree.command(description="Force-reset a key's HWID (ignores cooldown)")
@app_commands.guild_only()
@admin_only()
async def forcereset(i: discord.Interaction, key: str):
    q("UPDATE keys SET hwid=NULL, resets=resets+1, last_reset=? WHERE key=?", (int(time.time()), norm(key)))
    await i.response.send_message("✅ HWID cleared.", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
