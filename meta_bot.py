"""
Meta-bot: Telegram control panel for the live-bot library.

Long-polls Telegram, lets you spin up new live trading bots (each as its
own GitHub Actions workflow from bot-template.yml), list existing bots,
delete a bot's DB to restart it clean, and swap a bot's strategy/config
either by name (from the shared library) or by direct upload.

Runs inside GitHub Actions on the same restart-every-~6h pattern as the
live bots themselves. State (Telegram offset + in-progress conversation
flows) persists to the private repo so a restart doesn't lose context.
"""

import os
import sys
import time
import json
import base64
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# Backgrounded Python processes (this script invoked as `... &` for the
# watchdog and relay modes) get a PIPE for stdout, not a TTY — and Python
# only line-buffers on a TTY. Piped stdout is fully block-buffered (several
# KB), so print() output can sit invisible for the entire ~6h run instead
# of showing up in the GitHub Actions log. Reconfiguring here guarantees
# every print() everywhere in this file actually appears, regardless of
# how the process was invoked — this is the fix, not a workaround for one
# call site, since the same invisibility silently affected the watchdog's
# own prints too, not just the relay's.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# All scheduled times are interpreted in this timezone — change if you're
# ever not in Lagos when setting a schedule.
LAGOS_TZ = ZoneInfo("Africa/Lagos")

# ---------------------------------------------------------------------------
# Config — all from env, set by the workflow from repo secrets
# ---------------------------------------------------------------------------
META_BOT_TOKEN   = os.environ.get("META_BOT_TOKEN", "")
GITHUB_PAT       = os.environ.get("META_BOT_GITHUB_PAT", "")   # needs `repo` + `workflow` scopes
OWNER_CHAT_ID    = os.environ.get("OWNER_CHAT_ID", "")          # only this chat is ever obeyed
REPO_OWNER       = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
PUBLIC_REPO      = os.environ.get("PUBLIC_REPO", "wfo-scaffold")
PRIVATE_REPO     = "Miadol"

TG_API   = f"https://api.telegram.org/bot{META_BOT_TOKEN}"
GH_API   = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"token {GITHUB_PAT}",
    "Accept": "application/vnd.github.v3+json",
}

STATE_PATH = "freqtrade-core/user_data/meta_bot_state.json"
TEMPLATE_PATH = "bot-template.yml"  # lives in the public repo
STRATEGIES_DIR = "freqtrade-core/user_data/strategies"
CONFIGS_DIR = "freqtrade-core/user_data/configs"

STOP_AFTER_SECONDS = 5 * 3600 + 50 * 60  # mirrors bot-template.yml's own buffer

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=35)
    r.raise_for_status()
    return r.json().get("result")


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ New Bot"}, {"text": "📋 List Bots"}],
            [{"text": "📊 Status"}, {"text": "🔙 Back"}],
            [{"text": "ℹ️ Help"}, {"text": "🛠 Custom Bot"}],
        ],
        "resize_keyboard": True,
    }


def send(chat_id, text, inline=None, reply=True):
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if inline is not None:
        kwargs["reply_markup"] = {"inline_keyboard": inline}
    elif reply:
        kwargs["reply_markup"] = main_keyboard()
    tg("sendMessage", **kwargs)


def answer_callback(callback_id, text=""):
    tg("answerCallbackQuery", callback_query_id=callback_id, text=text)


def download_telegram_file(file_id):
    file_info = tg("getFile", file_id=file_id)
    path = file_info["file_path"]
    url = f"https://api.telegram.org/file/bot{META_BOT_TOKEN}/{path}"
    return requests.get(url, timeout=30).content

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def gh_get_file(repo, path):
    """Returns (content_bytes, sha) or (None, None) if it doesn't exist."""
    r = requests.get(f"{GH_API}/repos/{REPO_OWNER}/{repo}/contents/{path}",
                      headers=GH_HEADERS, timeout=20)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]), data["sha"]


def gh_put_file(repo, path, content_bytes, message, retries=3):
    """A 409 here means some other workflow (a live bot's periodic DB commit,
    another meta-bot action) pushed to this repo between our SHA fetch and
    our write — expected and increasingly common as more bots get added.
    Re-fetch the current SHA and retry rather than crashing the process."""
    last_error = None
    for attempt in range(retries):
        _, existing_sha = gh_get_file(repo, path)
        body = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode(),
        }
        if existing_sha:
            body["sha"] = existing_sha
        r = requests.put(f"{GH_API}/repos/{REPO_OWNER}/{repo}/contents/{path}",
                          headers=GH_HEADERS, json=body, timeout=20)
        if r.status_code == 409:
            last_error = r
            time.sleep(1 + attempt)  # brief backoff before refetching SHA and retrying
            continue
        r.raise_for_status()
        return r.json()
    last_error.raise_for_status()  # all retries exhausted — surface the real error


def gh_delete_file(repo, path, message):
    _, sha = gh_get_file(repo, path)
    if not sha:
        return False
    r = requests.delete(f"{GH_API}/repos/{REPO_OWNER}/{repo}/contents/{path}",
                         headers=GH_HEADERS,
                         json={"message": message, "sha": sha}, timeout=20)
    r.raise_for_status()
    return True


def gh_list_dir(repo, path):
    r = requests.get(f"{GH_API}/repos/{REPO_OWNER}/{repo}/contents/{path}",
                      headers=GH_HEADERS, timeout=20)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [item["name"] for item in r.json()]


def gh_dispatch(repo, event_type):
    requests.post(f"{GH_API}/repos/{REPO_OWNER}/{repo}/dispatches",
                  headers=GH_HEADERS,
                  json={"event_type": event_type}, timeout=20)


def list_live_bots():
    names = gh_list_dir(PUBLIC_REPO, ".github/workflows")
    return sorted(n[len("bot-"):-len(".yml")] for n in names
                  if n.startswith("bot-") and n.endswith(".yml"))


def bot_has_open_trades(bot_name):
    db_path = f"freqtrade-core/user_data/live_bots/{bot_name}/tradesv3.sqlite"
    content, _ = gh_get_file(PRIVATE_REPO, db_path)
    if content is None:
        return 0
    import sqlite3, tempfile
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as f:
        f.write(content)
        f.flush()
        try:
            con = sqlite3.connect(f.name)
            n = con.execute("SELECT COUNT(*) FROM trades WHERE is_open = 1").fetchone()[0]
            con.close()
            return n
        except Exception:
            return 0

# ---------------------------------------------------------------------------
# State persistence (offset + in-progress flows) — committed to private repo
# ---------------------------------------------------------------------------

def load_state():
    content, _ = gh_get_file(PRIVATE_REPO, STATE_PATH)
    if content is None:
        return {"offset": 0, "pending": {}}
    return json.loads(content)


def save_state(state):
    gh_put_file(PRIVATE_REPO, STATE_PATH,
                json.dumps(state, indent=2).encode(),
                "autosave: meta-bot state")

# ---------------------------------------------------------------------------
# Bot library actions
# ---------------------------------------------------------------------------

def list_custom_bots():
    names = gh_list_dir(PUBLIC_REPO, ".github/workflows")
    return sorted(n[len("custombot-"):-len(".yml")] for n in names
                  if n.startswith("custombot-") and n.endswith(".yml"))


def create_custom_bot(name, bot_token, py_content_bytes, requirements_content_bytes):
    bot_dir = f"freqtrade-core/user_data/custom_bots/{name}"
    gh_put_file(PRIVATE_REPO, f"{bot_dir}/bot.py", py_content_bytes,
                f"meta-bot: create custom bot {name}")
    gh_put_file(PRIVATE_REPO, f"{bot_dir}/requirements.txt", requirements_content_bytes,
                f"meta-bot: requirements for custom bot {name}")
    gh_put_file(PRIVATE_REPO, f"{bot_dir}/bot_token.txt", bot_token.encode(),
                f"meta-bot: token for custom bot {name}")
    # chat_id.txt — the runner exports this under all common env var names
    # (CHAT_ID, TELEGRAM_CHAT_ID, TITAN_CHAT_ID, TG_CHAT_ID) so any custom
    # bot script works regardless of what it calls its chat_id variable.
    gh_put_file(PRIVATE_REPO, f"{bot_dir}/chat_id.txt", OWNER_CHAT_ID.encode(),
                f"meta-bot: chat_id for custom bot {name}")

    template_bytes, _ = gh_get_file(PUBLIC_REPO, "custom-bot-template.yml")
    workflow = template_bytes.decode().replace("{{BOT_NAME}}", name)
    gh_put_file(PUBLIC_REPO, f".github/workflows/custombot-{name}.yml",
                workflow.encode(), f"meta-bot: create workflow for custom bot {name}")

    gh_dispatch(PUBLIC_REPO, f"restart-custombot-{name}")


def delete_custom_bot_entirely(name):
    gh_delete_file(PUBLIC_REPO, f".github/workflows/custombot-{name}.yml",
                    f"meta-bot: delete workflow for custom bot {name}")
    bot_dir = f"freqtrade-core/user_data/custom_bots/{name}"
    for fname in gh_list_dir(PRIVATE_REPO, bot_dir):
        gh_delete_file(PRIVATE_REPO, f"{bot_dir}/{fname}",
                        f"meta-bot: delete {fname} for custom bot {name} (full teardown)")


def delete_custom_bot_state(name):
    """Deletes everything except bot.py/requirements.txt/bot_token.txt/chat_id.txt —
    a fresh start for whatever state files the script itself created,
    without needing to know their names."""
    bot_dir = f"freqtrade-core/user_data/custom_bots/{name}"
    keep = {"bot.py", "requirements.txt", "bot_token.txt", "chat_id.txt"}
    for fname in gh_list_dir(PRIVATE_REPO, bot_dir):
        if fname not in keep:
            gh_delete_file(PRIVATE_REPO, f"{bot_dir}/{fname}",
                            f"meta-bot: clear state file {fname} for custom bot {name}")


def stop_custom_bot(name):
    path = f"freqtrade-core/user_data/custom_bots/{name}/control.json"
    gh_put_file(PRIVATE_REPO, path,
                json.dumps({"restart_requested": True, "paused": True}).encode(),
                f"meta-bot: stop custom bot {name}")


def resume_custom_bot(name):
    path = f"freqtrade-core/user_data/custom_bots/{name}/control.json"
    gh_put_file(PRIVATE_REPO, path,
                json.dumps({"restart_requested": False, "paused": False}).encode(),
                f"meta-bot: resume custom bot {name}")
    gh_dispatch(PUBLIC_REPO, f"restart-custombot-{name}")


def is_custom_bot_paused(name):
    path = f"freqtrade-core/user_data/custom_bots/{name}/control.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return False
    try:
        return json.loads(content).get("paused", False)
    except Exception:
        return False


def broadcast_webhook_block():
    """The webhook config block injected into every bot's secrets.json —
    factored out so create_bot() and ensure_broadcast_webhook() (the
    backfill for bots that existed before broadcast support) can't drift
    out of sync with each other.

    Every field below is verified against Freqtrade's ACTUAL internal RPC
    message dict, captured directly from a real run's logs (the literal
    {'type': 'exit_fill', 'trade_id': ..., ...} lines Freqtrade prints for
    every rpc_manager.send_msg call) — not from documentation, which can
    silently drift from a specific installed version. Freqtrade's webhook
    formatter does obj.format(**msg) against that exact dict; referencing
    a field name that ISN'T a real key throws KeyError inside Freqtrade's
    own webhook.py, and Freqtrade lets that exception escape rather than
    skip just the bad field — which kills the ENTIRE webhook call for that
    event. "strategy" and "max_stake_amount" were both never real keys in
    this version's message dict and were silently killing every single
    entry_fill/exit_fill webhook call, while "status" (whose only field,
    {status}, always exists) got through fine — which is exactly why
    relay_status.json only ever showed status events, never trades, even
    though the relay itself was working correctly the whole time.

    Deliberately does NOT include a "_version" key inside this dict —
    Freqtrade parses this whole structure, and we have direct proof from
    this exact bug that an extra/wrong key inside this section can cause
    real breakage (just at send-time, not at config-validation time, which
    is what made it so hard to spot). BROADCAST_WEBHOOK_VERSION (module
    level, below) is the source of truth instead, stored in secrets.json
    as a SEPARATE top-level field ("_broadcast_webhook_version") that
    Freqtrade never looks at, so bumping it can never risk feeding
    Freqtrade something it doesn't expect.
    """
    return {
        "enabled": True,
        "url": "http://127.0.0.1:9000/webhook",
        "format": "json",
        "retries": 2,
        "retry_delay": 1,
        "timeout": 10,
        "status": {
            "type": "status",
            "status": "{status}",
        },
        "entry_fill": {
            "type": "entry",
            "pair": "{pair}",
            "direction": "{direction}",
            "open_rate": "{open_rate}",
            "stake_amount": "{stake_amount}",
            "amount": "{amount}",
            "stake_currency": "{stake_currency}",
            "leverage": "{leverage}",
            "enter_tag": "{enter_tag}",
            "trade_id": "{trade_id}",
        },
        "exit_fill": {
            "type": "exit",
            "pair": "{pair}",
            "direction": "{direction}",
            "open_rate": "{open_rate}",
            "close_rate": "{close_rate}",
            "profit_ratio": "{profit_ratio}",
            "profit_amount": "{profit_amount}",
            "stake_currency": "{stake_currency}",
            "exit_reason": "{exit_reason}",
            "close_date": "{close_date}",
            "trade_id": "{trade_id}",
        },
        "entry_cancel": {
            "type": "entry_cancel",
            "pair": "{pair}",
            "direction": "{direction}",
            "trade_id": "{trade_id}",
        },
        "exit_cancel": {
            "type": "exit_cancel",
            "pair": "{pair}",
            "direction": "{direction}",
            "trade_id": "{trade_id}",
        },
    }


# Bump this any time broadcast_webhook_block()'s field set changes.
# Source of truth for whether a bot's secrets.json webhook config is
# current — stored OUTSIDE the webhook dict itself (see docstring above).
BROADCAST_WEBHOOK_VERSION = 2


def ensure_broadcast_webhook(name):
    """Backfill for bots created before broadcast support existed — their
    secrets.json has no webhook block at all, so the relay would never
    receive a single event even with destinations configured. Called
    automatically the first time the Broadcast menu is opened for a bot.

    Freqtrade only reads its config file at process startup — there is no
    live hot-patch for a section like `webhook` (the same reason
    request_reload_config / "Config reloaded" exists at all: Freqtrade's
    own /reload_config is a full internal restart, just one that doesn't
    drop open trades or require a new job). So writing the new webhook
    block to git is not enough by itself — if the bot is already running,
    its live process has no idea this file changed until something tells
    it to reload. This function does that automatically: if it just wrote
    a change, it also requests a reload, so the running process picks up
    the new webhook destination within ~2s rather than silently waiting
    for its next natural restart (up to ~6h away) before broadcast ever
    receives a single real trade event.

    Idempotent and safe to call on a bot that already has the CURRENT
    version — checked via the top-level "_broadcast_webhook_version" field
    on secrets.json (NOT inside the webhook dict — see broadcast_webhook_
    block()'s docstring for why), since a bot can already have a webhook
    block pointed at the right URL but with an outdated/broken field set
    (exactly what happened here: "strategy" and "max_stake_amount" were in
    the block from the start, pointed at the right URL the whole time, so
    a URL-only check would have permanently masked the fix from ever
    reaching already-backfilled bots).
    """
    path = f"freqtrade-core/user_data/live_bots/{name}/secrets.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return False
    data = json.loads(content)
    if data.get("_broadcast_webhook_version") == BROADCAST_WEBHOOK_VERSION:
        return False  # already current, nothing to do
    data["webhook"] = broadcast_webhook_block()
    data["_broadcast_webhook_version"] = BROADCAST_WEBHOOK_VERSION
    gh_put_file(PRIVATE_REPO, path, json.dumps(data, indent=2).encode(),
                f"meta-bot: backfill broadcast webhook config for {name}")
    request_reload_config(name)
    return True


def create_bot(name, bot_token, base_config, strategy_class, dry_run, api_key, api_secret):
    secrets_path = f"freqtrade-core/user_data/live_bots/{name}/secrets.json"
    secrets_content = json.dumps({
        "dry_run": dry_run,
        "exchange": {"key": api_key, "secret": api_secret},
        "telegram": {"enabled": True, "token": bot_token, "chat_id": OWNER_CHAT_ID},
        # Fixed, local-only override — guarantees the watchdog companion can
        # always reach this bot's API regardless of what the shared base
        # config does or doesn't set. 127.0.0.1 only: never reachable beyond
        # this one ephemeral runner, so these credentials carry no real risk
        # even though they're identical across every bot.
        "api_server": {
            "enabled": True,
            "listen_ip_address": "127.0.0.1",
            "listen_port": 8080,
            "username": "watchdog",
            "password": "watchdog",
            "verbosity": "error",
            "jwt_secret_key": "local-only-not-sensitive-32chars-min",
            "CORS_origins": [],
            "enable_openapi": False,
        },
        # Points at the relay subprocess (meta_bot.py relay mode, same
        # runner, started alongside the watchdog) — never a real network
        # hop. 127.0.0.1 used explicitly rather than "localhost": GitHub's
        # runner images add an IPv6 ::1 entry for "localhost" in /etc/hosts
        # on top of Ubuntu's normal 127.0.0.1-only default, and the relay
        # only binds IPv4 — using the literal IP sidesteps that resolution
        # ambiguity entirely rather than depending on resolver behavior.
        "webhook": broadcast_webhook_block(),
        "_broadcast_webhook_version": BROADCAST_WEBHOOK_VERSION,
    }, indent=2).encode()
    gh_put_file(PRIVATE_REPO, secrets_path, secrets_content,
                f"meta-bot: create secrets for {name}")

    template_bytes, _ = gh_get_file(PUBLIC_REPO, TEMPLATE_PATH)
    workflow = (template_bytes.decode()
                .replace("{{BOT_NAME}}", name)
                .replace("{{BASE_CONFIG}}", base_config)
                .replace("{{STRATEGY_CLASS}}", strategy_class))
    gh_put_file(PUBLIC_REPO, f".github/workflows/bot-{name}.yml",
                workflow.encode(), f"meta-bot: create workflow for {name}")

    gh_dispatch(PUBLIC_REPO, f"restart-bot-{name}")


def delete_bot_entirely(name):
    """Full teardown — the workflow file in public, and everything under
    this bot's folder in private. Not recoverable; the caller should have
    already confirmed with the person before calling this."""
    gh_delete_file(PUBLIC_REPO, f".github/workflows/bot-{name}.yml",
                    f"meta-bot: delete workflow for {name}")
    bot_dir = f"freqtrade-core/user_data/live_bots/{name}"
    for fname in gh_list_dir(PRIVATE_REPO, bot_dir):
        gh_delete_file(PRIVATE_REPO, f"{bot_dir}/{fname}",
                        f"meta-bot: delete {fname} for {name} (full teardown)")


def set_dry_run(name, dry_run):
    """Flip dry_run for an existing bot — overrides whatever the shared base
    config says, since this lives in the bot's own secrets.json, applied on
    its next restart (natural, scheduled, or via Restart Now)."""
    path = f"freqtrade-core/user_data/live_bots/{name}/secrets.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    data = json.loads(content)
    data["dry_run"] = dry_run
    gh_put_file(PRIVATE_REPO, path, json.dumps(data, indent=2).encode(),
                f"meta-bot: set dry_run={dry_run} for {name}")


def gh_delete_artifacts_by_name(repo, artifact_name):
    """Deletes every artifact matching artifact_name in `repo` — there can
    be more than one if retention overlapped a run, even with
    overwrite:true on upload. Returns count deleted. Best-effort: a 404 on
    an individual delete (already expired/gone) is not an error."""
    deleted = 0
    r = requests.get(f"{GH_API}/repos/{REPO_OWNER}/{repo}/actions/artifacts",
                      headers=GH_HEADERS, params={"name": artifact_name}, timeout=20)
    if r.status_code != 200:
        return deleted
    for artifact in r.json().get("artifacts", []):
        dr = requests.delete(f"{GH_API}/repos/{REPO_OWNER}/{repo}/actions/artifacts/{artifact['id']}",
                              headers=GH_HEADERS, timeout=20)
        if dr.status_code in (204, 404):
            deleted += 1
    return deleted


def delete_bot_db(name):
    """Wipes a bot's DB so its next restart genuinely starts clean. This
    has to clear THREE independent places state can otherwise survive in:

    1. The live tradesv3.sqlite in git (private repo) — what most people
       think of as "the DB".
    2. Its periodic git snapshot (tradesv3.sqlite.snapshot, committed every
       5 min while running) — added as a restore fallback for when the
       artifact step fails, but that means it ALSO silently restores a DB
       you just told it to delete, unless it's cleared too.
    3. The GitHub Actions artifact ({name}-db) in the public repo — the
       PRIMARY restore source on next restart. overwrite:true on upload
       means there's normally one, but it isn't touched by deleting #1 or
       #2 at all, so without this it would single-handedly undo this
       entire function on the very next restart.

    All three must go, or "Delete DB" only ever appears to work — exactly
    what was happening: deleting just the live file looked like a clean
    reset only because the artifact restore step was separately broken at
    the time. Fixing that bug resurrected this one.
    """
    bot_dir = f"freqtrade-core/user_data/live_bots/{name}"
    gh_delete_file(PRIVATE_REPO, f"{bot_dir}/tradesv3.sqlite",
                    f"meta-bot: delete DB for {name} (clean restart)")
    gh_delete_file(PRIVATE_REPO, f"{bot_dir}/tradesv3.sqlite.snapshot",
                    f"meta-bot: delete DB snapshot for {name} (clean restart)")
    n = gh_delete_artifacts_by_name(PUBLIC_REPO, f"{name}-db")
    return n  # number of artifacts removed, for the confirmation message


def update_bot_field(name, field_placeholder_pairs):
    """field_placeholder_pairs: list of (env_var_name, new_value) to rewrite
    in that bot's own committed workflow file."""
    path = f".github/workflows/bot-{name}.yml"
    content, _ = gh_get_file(PUBLIC_REPO, path)
    text = content.decode()
    for env_name, new_value in field_placeholder_pairs:
        import re
        text = re.sub(rf'{env_name}: ".*?"', f'{env_name}: "{new_value}"', text)
    gh_put_file(PUBLIC_REPO, path, text.encode(), f"meta-bot: update {name}")

# ---------------------------------------------------------------------------
# Scheduled tasks — per bot only, never global. Mirrors watchdog's
# ScheduledTask shape: enabled + a list of "HH:MM" times + last_run_times
# (keyed by time string -> date string) so a task fires once per time per
# day, not on every ~30s poll that happens to land within that minute.
# ---------------------------------------------------------------------------

def get_watchdog_config(name):
    path = f"freqtrade-core/user_data/live_bots/{name}/watchdog_config.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return {
            "alerts_enabled": False,
            "drawdown_alert_pct": None, "drawdown_cap_pct": None,
            "peak_alert_levels": [], "peak_cap_pct": None,
            "reset_requested": False,
        }
    cfg = json.loads(content)
    cfg.setdefault("reset_requested", False)
    return cfg


def save_watchdog_config(name, cfg):
    path = f"freqtrade-core/user_data/live_bots/{name}/watchdog_config.json"
    gh_put_file(PRIVATE_REPO, path, json.dumps(cfg, indent=2).encode(),
                f"meta-bot: update watchdog config for {name}")


def request_hwm_reset(name):
    """Sets a flag in the SETTINGS file (which the running watchdog already
    re-reads every poll), rather than touching its state file directly —
    the watchdog owns its own state and clears this flag itself once it has
    actually reset, so there's no race between us writing state and it
    writing state at the same time."""
    cfg = get_watchdog_config(name)
    cfg["reset_requested"] = True
    save_watchdog_config(name, cfg)


def get_watchdog_state(name):
    path = f"freqtrade-core/user_data/live_bots/{name}/watchdog_state.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return None
    try:
        return json.loads(content)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Broadcast — per-bot list of destinations (Telegram chats and/or raw
# webhook URLs), each independently scaled, filtered, and pausable.
# Config lives next to watchdog_config.json / control.json, same git-backed
# pattern — read by the relay subprocess started alongside the watchdog.
# ---------------------------------------------------------------------------

# Every field a destination can choose to include. "event:*" gate whether
# that event type is sent at all; the rest gate which data fields are
# attached when an entry/exit event *is* sent. Kept flat (not nested) so
# toggling one is a single dict key, and so new fields can be appended
# without migrating old configs (missing keys just fall back via .get()).
BROADCAST_EVENT_FIELDS = [
    ("event_entry_fill",   "Entry filled"),
    ("event_entry_cancel", "Entry cancelled"),
    ("event_exit_fill",    "Exit filled"),
    ("event_exit_cancel",  "Exit cancelled"),
    ("event_status",       "Bot status (started/stopped)"),
    ("event_reload",       "Config reloaded"),
]
BROADCAST_DATA_FIELDS = [
    ("show_leverage",    "Leverage"),
    ("show_stake",       "Stake amount"),
    ("show_fills",       "Fill price/amount detail"),
    ("show_tags",        "Enter/exit tags"),
]
BROADCAST_DEFAULT_FIELDS = {k: True for k, _ in BROADCAST_EVENT_FIELDS}
BROADCAST_DEFAULT_FIELDS.update({
    "show_leverage": True,
    "show_stake": False,    # conservative default — reveals account scale
    "show_fills": True,
    "show_tags": True,
})


def get_broadcast_config(name):
    path = f"freqtrade-core/user_data/live_bots/{name}/broadcast_config.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return {"enabled": False, "stagger_seconds": 1.0, "followable": False, "destinations": {}}
    cfg = json.loads(content)
    cfg.setdefault("enabled", False)
    cfg.setdefault("stagger_seconds", 1.0)
    # Whether this bot is offered as a choice in the automate-bot's "/follow"
    # picker (built separately, later) — kept here rather than as its own
    # file since it's fundamentally the same concern as the rest of this
    # config: what this bot exposes externally, and to whom. A bot can be
    # followable with zero destinations configured (followable governs the
    # picker; it doesn't add the automate-bot as a destination by itself —
    # that's still a deliberate one-time step, same as adding any other
    # destination, so a bot never becomes followable by accident).
    cfg.setdefault("followable", False)
    cfg.setdefault("destinations", {})
    for dest in cfg["destinations"].values():
        dest.setdefault("active", True)
        dest.setdefault("scale", 1.0)
        dest.setdefault("stagger", dest.get("type") == "telegram")
        fields = dest.setdefault("fields", {})
        for key, _ in BROADCAST_EVENT_FIELDS + BROADCAST_DATA_FIELDS:
            fields.setdefault(key, BROADCAST_DEFAULT_FIELDS[key])
    return cfg


def list_followable_bots():
    """The registry the automate-bot's "/follow" picker reads from — every
    bot with followable=True in its broadcast_config.json, regardless of
    whether it currently has any destinations configured. Returns a sorted
    list of bot names. This is the one piece of plumbing that lets a new
    followable bot 'just work' for automate-bot without any change to
    automate-bot's own code: it queries this same private repo (however it
    ends up authenticating — same PAT pattern as everything else here),
    not a hardcoded list baked into its source."""
    followable = []
    for name in list_live_bots():
        cfg = get_broadcast_config(name)
        if cfg.get("followable"):
            followable.append(name)
    return sorted(followable)


def save_broadcast_config(name, cfg):
    path = f"freqtrade-core/user_data/live_bots/{name}/broadcast_config.json"
    gh_put_file(PRIVATE_REPO, path, json.dumps(cfg, indent=2).encode(),
                f"meta-bot: update broadcast config for {name}")


def new_destination_id(cfg):
    """Short, stable, collision-free id — used in callback_data, so it
    needs to be compact (Telegram caps callback_data at 64 bytes total,
    and we prefix it with action names + bot name already)."""
    import uuid
    while True:
        did = uuid.uuid4().hex[:8]
        if did not in cfg["destinations"]:
            return did


def kb_broadcast_menu(name, cfg):
    enabled_icon = "✅ ON" if cfg["enabled"] else "⚪ OFF"
    followable_icon = "✅ ON" if cfg.get("followable") else "⚪ OFF"
    rows = [
        [{"text": f"Broadcast: {enabled_icon}", "callback_data": f"bc_toggle_enabled:{name}"}],
        [{"text": f"⏱ Telegram stagger: {cfg['stagger_seconds']:g}s",
          "callback_data": f"bc_stagger_edit:{name}"}],
        [{"text": "🩺 Relay status", "callback_data": f"bc_relay_status:{name}"}],
        [{"text": f"👥 Followable (automate-bot): {followable_icon}",
          "callback_data": f"bc_toggle_followable:{name}"}],
    ]
    for did, dest in cfg["destinations"].items():
        icon = "📡" if dest["type"] == "telegram" else "🔗"
        state_icon = "▶️" if dest["active"] else "⏸"
        scale_txt = f"{dest['scale']:g}x"
        rows.append([{"text": f"{icon} {dest['label']} ({scale_txt}) {state_icon}",
                       "callback_data": f"bc_dest_open:{name}:{did}"}])
    rows.append([{"text": "➕ New destination", "callback_data": f"bc_dest_new:{name}"}])
    rows.append([{"text": "🔙 Back", "callback_data": "backtolist"}])
    return rows


def kb_broadcast_dest_menu(name, did, dest):
    state_icon = "⏸ Pause" if dest["active"] else "▶️ Resume"
    stagger_icon = "✅ ON" if dest["stagger"] else "⚪ OFF"
    fields = dest["fields"]

    rows = [
        [{"text": f"Type: {dest['type']} — {dest['label']}", "callback_data": "noop"}],
        [{"text": state_icon, "callback_data": f"bc_dest_toggleactive:{name}:{did}"},
         {"text": "✏️ Rename", "callback_data": f"bc_dest_rename:{name}:{did}"}],
        [{"text": f"⚖️ Scale: {dest['scale']:g}x", "callback_data": f"bc_dest_scale:{name}:{did}"}],
    ]
    if dest["type"] == "webhook":
        rows.append([{"text": "🔧 Edit URL", "callback_data": f"bc_dest_editurl:{name}:{did}"}])
    else:
        rows.append([{"text": "🔧 Edit chat ID", "callback_data": f"bc_dest_editchatid:{name}:{did}"}])
        rows.append([{"text": f"Stagger: {stagger_icon}",
                       "callback_data": f"bc_dest_togglestagger:{name}:{did}"}])

    rows.append([{"text": "— Events —", "callback_data": "noop"}])
    for key, label in BROADCAST_EVENT_FIELDS:
        icon = "✅" if fields[key] else "⚪"
        rows.append([{"text": f"{icon} {label}", "callback_data": f"bc_field_toggle:{name}:{did}:{key}"}])

    rows.append([{"text": "— Fields included —", "callback_data": "noop"}])
    for key, label in BROADCAST_DATA_FIELDS:
        icon = "✅" if fields[key] else "⚪"
        rows.append([{"text": f"{icon} {label}", "callback_data": f"bc_field_toggle:{name}:{did}:{key}"}])

    rows.append([{"text": "👁 Preview", "callback_data": f"bc_dest_preview:{name}:{did}"},
                 {"text": "🧪 Send test message", "callback_data": f"bc_dest_test:{name}:{did}"}])
    rows.append([{"text": "🗑 Remove destination", "callback_data": f"bc_dest_delete_confirm:{name}:{did}"}])
    rows.append([{"text": "🔙 Back to broadcast list", "callback_data": f"bc_menu:{name}"}])
    return rows


# Sample payload used for "Preview" — shaped exactly like what Freqtrade's
# webhook actually sends for entry_fill/exit_fill (see field list in
# https://www.freqtrade.io/en/stable/webhook-config/), so what you see in
# preview matches what a real trade event will produce, field-for-field.
BROADCAST_SAMPLE_TRADE = {
    "entry_fill": {
        "type": "entry_fill", "pair": "BTC/USDT:USDT", "direction": "long",
        "leverage": 10, "open_rate": 64250.5, "amount": 0.0156,
        "stake_amount": 100.0, "trade_id": 4821, "enter_tag": "sample-signal",
    },
    "exit_fill": {
        "type": "exit_fill", "pair": "BTC/USDT:USDT", "direction": "long",
        "leverage": 10, "open_rate": 64250.5, "close_rate": 65100.0,
        "amount": 0.0156, "stake_amount": 100.0, "profit_ratio": 0.0823,
        "profit_amount": 13.25, "trade_id": 4821, "exit_reason": "roi",
        "close_date": "2026-06-23 10:14:02",
    },
}


def render_broadcast_message(event_type, data, dest_fields, scale=1.0):
    """Builds the HTML text for one event, respecting one destination's
    field toggles and stake scale. Returns None if this event type is
    switched off for this destination (caller should skip sending).

    `scale` only ever multiplies the STAKE AMOUNT shown — not leverage,
    not price, not profit %. Percentage returns are scale-invariant by
    construction; what changes is purely "how much capital this represents
    at this destination's chosen account size", which is the only thing
    scale is supposed to mean.
    """
    # NOTE: Freqtrade's webhook config (as configured in secrets.json,
    # format=json) sends "type": "entry" for entry_fill and "type": "exit"
    # for exit_fill — "entry_fill"/"exit_fill" are accepted too as aliases,
    # in case a config sends the more literal name instead. Both gate the
    # same toggle either way.
    event_gate = {
        "entry": "event_entry_fill", "entry_fill": "event_entry_fill",
        "entry_cancel": "event_entry_cancel",
        "exit": "event_exit_fill", "exit_fill": "event_exit_fill",
        "exit_cancel": "event_exit_cancel",
        "status": "event_status", "reload": "event_reload",
    }.get(event_type)
    if event_gate and not dest_fields.get(event_gate, True):
        return None

    if event_type == "status":
        return f"<b>🔦 Bot Status:</b> {data.get('status')}"
    if event_type == "reload":
        return f"<b>⚙️ Config reloaded</b> — took effect in-place, no restart."

    pair = data.get("pair", "?")
    direction = str(data.get("direction", "")).upper()
    trade_id = data.get("trade_id", "?")
    lines = []

    if event_type in ("entry", "entry_fill"):
        lines = [f"<b>🚀 ENTRY FILLED</b>", "", f"Pair: <b>{pair}</b>", f"Direction: <b>{direction}</b>"]
        if dest_fields.get("show_leverage", True) and data.get("leverage"):
            lines.append(f"Leverage: <b>{data['leverage']}x</b>")
        if dest_fields.get("show_fills", True) and data.get("open_rate") is not None:
            lines.append(f"Entry Price: <b>{data['open_rate']}</b>")
        if dest_fields.get("show_stake", False) and data.get("stake_amount") is not None:
            lines.append(f"Stake: <b>{float(data['stake_amount']) * scale:.2f}</b> (scaled {scale:g}x)")
        if dest_fields.get("show_tags", True) and data.get("enter_tag"):
            lines.append(f"Tag: <b>{data['enter_tag']}</b>")
        lines.append(f"Trade ID: <b>{trade_id}</b>")

    elif event_type == "entry_cancel":
        lines = [f"<b>❌ ENTRY CANCELLED</b>", "", f"Pair: <b>{pair}</b>",
                 f"Direction: <b>{direction}</b>", f"Trade ID: <b>{trade_id}</b>"]

    elif event_type in ("exit", "exit_fill"):
        profit_pct = float(data.get("profit_ratio", 0) or 0) * 100
        profit_emoji = "🟢" if profit_pct >= 0 else "🔴"
        lines = [f"<b>📊 TRADE CLOSED</b>", "", f"Pair: <b>{pair}</b>", f"Direction: <b>{direction}</b>"]
        if dest_fields.get("show_leverage", True) and data.get("leverage"):
            lines.append(f"Leverage: <b>{data['leverage']}x</b>")
        if dest_fields.get("show_fills", True):
            if data.get("open_rate") is not None:
                lines.append(f"Entry: <b>{data['open_rate']}</b>")
            if data.get("close_rate") is not None:
                lines.append(f"Exit: <b>{data['close_rate']}</b>")
        lines.append(f"Profit: <b>{profit_emoji} {profit_pct:.2f}%</b>")
        if dest_fields.get("show_stake", False) and data.get("stake_amount") is not None:
            lines.append(f"Stake: <b>{float(data['stake_amount']) * scale:.2f}</b> (scaled {scale:g}x)")
        if dest_fields.get("show_tags", True) and data.get("exit_reason"):
            lines.append(f"Exit Reason: <b>{data['exit_reason']}</b>")
        lines.append(f"Trade ID: <b>{trade_id}</b>")
        if data.get("close_date"):
            lines.append(f"Close Time (UTC): <b>{data['close_date']}</b>")

    elif event_type == "exit_cancel":
        lines = [f"<b>⚠️ EXIT CANCELLED</b>", "", f"Pair: <b>{pair}</b>",
                 f"Direction: <b>{direction}</b>", f"Trade ID: <b>{trade_id}</b>"]

    else:
        return None  # unknown event type — never broadcast

    return "\n".join(lines)


def kb_drawdown_menu(name, cfg):
    enabled_icon = "✅ ON" if cfg["alerts_enabled"] else "⚪ OFF"
    dd_alert = f"{cfg['drawdown_alert_pct']:.1f}%" if cfg["drawdown_alert_pct"] else "Off"
    dd_cap = f"{cfg['drawdown_cap_pct']:.1f}%" if cfg["drawdown_cap_pct"] else "Off"
    peak_cap = f"{cfg['peak_cap_pct']:.1f}%" if cfg["peak_cap_pct"] else "Off"
    peak_levels = ", ".join(f"{l:g}%" for l in cfg["peak_alert_levels"]) or "None"

    return [
        [{"text": f"Monitoring: {enabled_icon}", "callback_data": f"wd_toggle_enabled:{name}"}],
        [{"text": "📉 Drawdown Alert", "callback_data": "noop"}],
        [{"text": "➖", "callback_data": f"wd_ddalert_dec:{name}"},
         {"text": dd_alert, "callback_data": f"wd_ddalert_direct:{name}"},
         {"text": "➕", "callback_data": f"wd_ddalert_inc:{name}"}],
        [{"text": "🛑 Drawdown Cap (auto-close)", "callback_data": "noop"}],
        [{"text": "➖", "callback_data": f"wd_ddcap_dec:{name}"},
         {"text": dd_cap, "callback_data": f"wd_ddcap_direct:{name}"},
         {"text": "➕", "callback_data": f"wd_ddcap_inc:{name}"}],
        [{"text": "📈 Peak Alerts (multiple, fire once each)", "callback_data": "noop"}],
        [{"text": f"Levels: {peak_levels[:30]}", "callback_data": f"wd_peak_edit:{name}"}],
        [{"text": "🚀 Peak Cap (auto-close, only one)", "callback_data": "noop"}],
        [{"text": "➖", "callback_data": f"wd_peakcap_dec:{name}"},
         {"text": peak_cap, "callback_data": f"wd_peakcap_direct:{name}"},
         {"text": "➕", "callback_data": f"wd_peakcap_inc:{name}"}],
        [{"text": "♻️ Reset HWM (after a cap trigger)", "callback_data": f"wd_reset:{name}"}],
        [{"text": "🔙 Back", "callback_data": "backtolist"}],
    ]


def get_bot_schedule(state, name):
    schedules = state.setdefault("schedules", {})
    return schedules.setdefault(name, {
        "delete_db": {"enabled": False, "times": [], "last_run": {}},
        "reload_config": {"enabled": False, "times": [], "last_run": {}},
        "hyperopt": {"enabled": False, "times": [], "last_run": {}},
    })


def should_run(task, time_str, date_str):
    if not task["enabled"] or time_str not in task["times"]:
        return False
    return task["last_run"].get(time_str) != date_str


def mark_as_run(task, time_str, date_str):
    task["last_run"][time_str] = date_str


def set_control_flag(name):
    """Signal a currently-running bot to stop itself gracefully and let its
    own if:always() restart step bring it back up. We never kill a run from
    out here directly — only the bot itself decides it's safe to stop, which
    is what guarantees there's never a moment with two instances running."""
    path = f"freqtrade-core/user_data/live_bots/{name}/control.json"
    gh_put_file(PRIVATE_REPO, path,
                json.dumps({"restart_requested": True, "paused": False}).encode(),
                f"meta-bot: request early restart for {name}")


def request_reload_config(name):
    """Sets a lightweight flag that the running watchdog picks up within
    ~2s and converts into a direct /reload_config API call to Freqtrade —
    no stop/start, no job restart, no 5-minute wait. Freqtrade reloads
    its config in-place via its own native endpoint."""
    cfg = get_watchdog_config(name)
    cfg["reload_config_requested"] = True
    save_watchdog_config(name, cfg)


def get_control(name):
    path = f"freqtrade-core/user_data/live_bots/{name}/control.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return {"restart_requested": False, "paused": False}
    try:
        return json.loads(content)
    except Exception:
        return {"restart_requested": False, "paused": False}


def is_bot_paused(name):
    return get_control(name).get("paused", False)


def stop_bot(name):
    """Ends this bot's restart chain. The currently running instance still
    finishes its own graceful shutdown on its normal ~5min check-in — we're
    not force-killing it — but its own 'Schedule next run' step will see the
    paused flag and skip dispatching a new run."""
    path = f"freqtrade-core/user_data/live_bots/{name}/control.json"
    gh_put_file(PRIVATE_REPO, path,
                json.dumps({"restart_requested": True, "paused": True}).encode(),
                f"meta-bot: stop {name}")


def resume_bot(name):
    """Clears the pause and starts a fresh run directly, since there's no
    longer a running instance left to self-restart from."""
    path = f"freqtrade-core/user_data/live_bots/{name}/control.json"
    gh_put_file(PRIVATE_REPO, path,
                json.dumps({"restart_requested": False, "paused": False}).encode(),
                f"meta-bot: resume {name}")
    gh_dispatch(PUBLIC_REPO, f"restart-bot-{name}")


def get_hyperopt_config(name):
    path = f"freqtrade-core/user_data/live_bots/{name}/hyperopt_config.json"
    content, _ = gh_get_file(PRIVATE_REPO, path)
    if content is None:
        return {
            "loss_function": "SharpeHyperOptLoss", "lookback_days": 90,
            "epochs": 500, "early_stop": 0, "random_state": 42,
            "warm_start_mode": "warm", "reload_after": False,
            "spaces": "all",
        }
    return json.loads(content)


def save_hyperopt_config(name, cfg):
    path = f"freqtrade-core/user_data/live_bots/{name}/hyperopt_config.json"
    gh_put_file(PRIVATE_REPO, path, json.dumps(cfg, indent=2).encode(),
                f"meta-bot: update hyperopt config for {name}")


def trigger_hyperopt(name):
    """Dispatches the shared Hyperopt Runner workflow with this bot's
    current parameters. Reads BASE_CONFIG/STRATEGY_CLASS straight out of
    the bot's own committed workflow file, so it always matches whatever
    that bot is actually running right now — never goes stale relative to
    a Strategy/Config swap done through the meta-bot."""
    path = f".github/workflows/bot-{name}.yml"
    content, _ = gh_get_file(PUBLIC_REPO, path)
    text = content.decode()
    import re
    base_config = re.search(r'BASE_CONFIG: "(.*?)"', text).group(1)
    strategy_class = re.search(r'STRATEGY_CLASS: "(.*?)"', text).group(1)

    cfg = get_hyperopt_config(name)
    payload = {
        "bot_name": name, "strategy_class": strategy_class, "base_config": base_config,
        "loss_function": cfg["loss_function"], "lookback_days": cfg["lookback_days"],
        "epochs": cfg["epochs"], "early_stop": cfg["early_stop"],
        "random_state": cfg["random_state"], "warm_start_mode": cfg["warm_start_mode"],
        "reload_after": cfg["reload_after"],
        "spaces": cfg.get("spaces", "all").split(),
    }
    if cfg["warm_start_mode"] == "upload":
        payload["upload_path"] = f"freqtrade-core/user_data/live_bots/{name}/hyperopt_warmstart_upload.json"

    requests.post(f"{GH_API}/repos/{REPO_OWNER}/{PUBLIC_REPO}/dispatches",
                  headers=GH_HEADERS,
                  json={"event_type": "run-hyperopt", "client_payload": payload}, timeout=20)


def execute_scheduled_delete_db(state, name):
    open_trades = bot_has_open_trades(name)
    delete_bot_db(name)
    set_control_flag(name)
    note = f" (had {open_trades} open trade — exchange stop is its only protection now until restart)" if open_trades else ""
    send(OWNER_CHAT_ID, f"⏰ Scheduled DB delete fired for <b>{name}</b>{note}. Restarting clean shortly.")


def execute_scheduled_reload_config(state, name):
    # FIX: instead of a full stop/restart (~5min), set a flag the watchdog
    # picks up within ~2s and converts to a direct /reload_config API call.
    request_reload_config(name)
    send(OWNER_CHAT_ID, f"⏰ Scheduled config reload fired for <b>{name}</b>. Taking effect within seconds via in-place reload.")


def check_schedules(state):
    now = datetime.now(LAGOS_TZ)
    time_str = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")
    changed = False

    for name, sched in state.get("schedules", {}).items():
        delete_task = sched["delete_db"]
        if should_run(delete_task, time_str, date_str):
            mark_as_run(delete_task, time_str, date_str)
            execute_scheduled_delete_db(state, name)
            changed = True

        reload_task = sched["reload_config"]
        if should_run(reload_task, time_str, date_str):
            mark_as_run(reload_task, time_str, date_str)
            execute_scheduled_reload_config(state, name)
            changed = True

        hyperopt_task = sched["hyperopt"]
        if should_run(hyperopt_task, time_str, date_str):
            mark_as_run(hyperopt_task, time_str, date_str)
            trigger_hyperopt(name)
            send(OWNER_CHAT_ID, f"⏰ Scheduled hyperopt fired for <b>{name}</b> — running now, "
                                  f"this can take a while.")
            changed = True

    if changed:
        save_state(state)

# ---------------------------------------------------------------------------
# Flow handlers
# ---------------------------------------------------------------------------

def get_latest_run_status(workflow_filename):
    """🟢 active, 🟡 respawning (between cycles, dispatch should be inbound),
    🔴 stalled (last run failed/cancelled — its restart chain may be broken),
    or None if it's never run yet."""
    r = requests.get(f"{GH_API}/repos/{REPO_OWNER}/{PUBLIC_REPO}/actions/workflows/{workflow_filename}/runs",
                      headers=GH_HEADERS, params={"per_page": 1}, timeout=20)
    if r.status_code != 200:
        return None
    runs = r.json().get("workflow_runs", [])
    if not runs:
        return None
    run = runs[0]
    if run["status"] in ("in_progress", "queued"):
        return "🟢 active"
    if run["status"] == "completed" and run["conclusion"] == "success":
        return "🟡 respawning"
    return "🔴 stalled"


def show_status(chat_id):
    bots = list_live_bots()
    if not bots:
        send(chat_id, "No bots in the library yet.")
        return
    lines = []
    for name in bots:
        if is_bot_paused(name):
            status = "⏸ paused"
        else:
            status = get_latest_run_status(f"bot-{name}.yml") or "❔ unknown"
        line = f"<b>{name}</b> — {status}"

        wd_state = get_watchdog_state(name)
        if wd_state and wd_state.get("last_equity") is not None:
            equity = wd_state["last_equity"]
            hwm = wd_state.get("hwm", 0)
            age_sec = time.time() - wd_state.get("last_updated_ts", 0)
            dd_pct = ((hwm - equity) / hwm * 100) if hwm else 0
            line += (f"\n  💰 ${equity:.2f}  📉 {dd_pct:.2f}% off peak  "
                      f"<i>(as of {int(age_sec)}s ago)</i>")
        lines.append(line)
    send(chat_id, "\n\n".join(lines))


def show_bot_list(chat_id):
    bots = list_live_bots()
    custom_bots = list_custom_bots()
    if not bots and not custom_bots:
        send(chat_id, "No bots in the library yet. Tap ➕ New Bot or 🛠 Custom Bot to create one.")
        return
    for name in bots:
        paused = is_bot_paused(name)
        run_btn = ({"text": "▶️ Resume", "callback_data": f"resume_bot:{name}"} if paused
                   else {"text": "⏹ Stop", "callback_data": f"stop_bot:{name}"})
        send(chat_id, f"🤖 <b>{name}</b> {'⏸ PAUSED' if paused else ''}", inline=[
            [{"text": "🗑 Delete DB", "callback_data": f"delete:{name}"},
             {"text": "🔄 Strategy", "callback_data": f"strat:{name}"},
             {"text": "⚙️ Config", "callback_data": f"config:{name}"}],
            [{"text": "⏰ Schedule", "callback_data": f"sched_menu:{name}"},
             {"text": "🔁 Restart Now", "callback_data": f"restart_now:{name}"}],
            [run_btn, {"text": "🧪/🔴 Toggle Dry Run", "callback_data": f"toggle_dryrun:{name}"}],
            [{"text": "📉 Drawdown", "callback_data": f"dd_menu:{name}"},
             {"text": "📡 Broadcast", "callback_data": f"bc_menu:{name}"}],
            [{"text": "💀 Delete Entirely", "callback_data": f"delete_entirely_confirm:{name}"}],
        ])
    for name in custom_bots:
        paused = is_custom_bot_paused(name)
        run_btn = ({"text": "▶️ Resume", "callback_data": f"cb_resume:{name}"} if paused
                   else {"text": "⏹ Stop", "callback_data": f"cb_stop:{name}"})
        send(chat_id, f"🛠 <b>{name}</b> (custom) {'⏸ PAUSED' if paused else ''}", inline=[
            [run_btn, {"text": "🗑 Delete State", "callback_data": f"cb_delete_state:{name}"}],
            [{"text": "⬆️ New .py", "callback_data": f"cb_upload_py:{name}"},
             {"text": "⬆️ New requirements.txt", "callback_data": f"cb_upload_req:{name}"}],
            [{"text": "💀 Delete Entirely", "callback_data": f"cb_delete_entirely_confirm:{name}"}],
        ])


def kb_schedule_menu(name, sched):
    delete_task = sched["delete_db"]
    reload_task = sched["reload_config"]
    hyperopt_task = sched["hyperopt"]
    delete_icon = "✅ ON" if delete_task["enabled"] else "⚪ OFF"
    reload_icon = "✅ ON" if reload_task["enabled"] else "⚪ OFF"
    hyperopt_icon = "✅ ON" if hyperopt_task["enabled"] else "⚪ OFF"
    delete_times = ", ".join(delete_task["times"]) if delete_task["times"] else "None"
    reload_times = ", ".join(reload_task["times"]) if reload_task["times"] else "None"
    hyperopt_times = ", ".join(hyperopt_task["times"]) if hyperopt_task["times"] else "None"

    hcfg = get_hyperopt_config(name)
    reload_after_icon = "✅ ON" if hcfg["reload_after"] else "⚪ OFF"

    return [
        [{"text": "🗑 Auto Delete-DB", "callback_data": "noop"}],
        [{"text": f"{delete_icon}", "callback_data": f"sched_delete_toggle:{name}"},
         {"text": f"Times: {delete_times[:30]}", "callback_data": f"sched_delete_edit:{name}"}],
        [{"text": "⚙️ Auto Reload-Config", "callback_data": "noop"}],
        [{"text": f"{reload_icon}", "callback_data": f"sched_reload_toggle:{name}"},
         {"text": f"Times: {reload_times[:30]}", "callback_data": f"sched_reload_edit:{name}"}],
        [{"text": "🧬 Hyperopt — schedule", "callback_data": "noop"}],
        [{"text": f"{hyperopt_icon}", "callback_data": f"sched_hyperopt_toggle:{name}"},
         {"text": f"Times: {hyperopt_times[:30]}", "callback_data": f"sched_hyperopt_edit:{name}"}],
        [{"text": "🧬 Hyperopt — parameters", "callback_data": "noop"}],
        [{"text": f"Loss: {hcfg['loss_function']}", "callback_data": f"hp_loss:{name}"}],
        [{"text": f"Lookback: {hcfg['lookback_days']}d", "callback_data": f"hp_lookback:{name}"},
         {"text": f"Epochs: {hcfg['epochs']}", "callback_data": f"hp_epochs:{name}"}],
        [{"text": f"Early-stop: {hcfg['early_stop'] or 'off'}", "callback_data": f"hp_earlystop:{name}"},
         {"text": f"Seed: {hcfg['random_state']}", "callback_data": f"hp_seed:{name}"}],
        [{"text": f"Start: {hcfg['warm_start_mode']}", "callback_data": f"hp_startmode:{name}"}],
        [{"text": f"Spaces: {hcfg.get('spaces', 'all')}", "callback_data": f"hp_spaces:{name}"}],
        [{"text": f"Reload-after: {reload_after_icon}", "callback_data": f"hp_reloadafter_toggle:{name}"}],
        [{"text": "🚀 Run Hyperopt Now", "callback_data": f"hp_run_now:{name}"}],
        [{"text": "🔙 Back", "callback_data": "backtolist"}],
    ]


def handle_callback(state, cq):
    chat_id = str(cq["message"]["chat"]["id"])
    if chat_id != OWNER_CHAT_ID:
        return
    data = cq["data"]
    answer_callback(cq["id"])

    if data.startswith("delete:"):
        name = data.split(":", 1)[1]
        open_trades = bot_has_open_trades(name)
        if open_trades:
            send(chat_id,
                 f"⚠️ <b>{name}</b> has {open_trades} open trade(s) right now. "
                 f"Deleting the DB means the bot forgets about them next restart — "
                 f"the exchange-side stop still protects the position, but nothing "
                 f"will be tracking or managing it anymore.\n\nSend /confirm_delete_{name} "
                 f"to proceed anyway, or flatten the position first.")
        else:
            n_artifacts = delete_bot_db(name)
            artifact_note = f", {n_artifacts} stale artifact(s) cleared" if n_artifacts else ""
            send(chat_id, f"✅ DB deleted for {name} (live file + git snapshot{artifact_note}). "
                           f"Clean slate on its next restart — nothing left for it to restore from.")

    elif data.startswith("strat:"):
        name = data.split(":", 1)[1]
        strategies = gh_list_dir(PRIVATE_REPO, STRATEGIES_DIR)
        buttons = [[{"text": s, "callback_data": f"setstrat:{name}:{s}"}]
                   for s in strategies if s.endswith(".py")]
        buttons.append([{"text": "✏️ Type it myself", "callback_data": f"typestrat:{name}"}])
        buttons.append([{"text": "⬆️ Upload new strategy file", "callback_data": f"uploadstrat:{name}"}])
        buttons.append([{"text": "🔙 Back", "callback_data": "backtolist"}])
        send(chat_id, f"Pick a strategy for <b>{name}</b>:", inline=buttons)

    elif data.startswith("typestrat:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_strategy_text", "bot": name}
        send(chat_id, f"Type the exact strategy class name for {name} (no .py, must match "
                       f"a class already in the strategies folder).")

    elif data.startswith("setstrat:"):
        _, name, strategy_file = data.split(":", 2)
        strategy_class = strategy_file[:-3]  # filename without .py, assumes class name matches
        update_bot_field(name, [("STRATEGY_CLASS", strategy_class)])
        send(chat_id, f"✅ {name} will use {strategy_class} on its next restart.")

    elif data.startswith("uploadstrat:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_strategy_upload", "bot": name}
        send(chat_id, f"Send the .py strategy file now for {name}.")

    elif data.startswith("stop_bot:"):
        name = data.split(":", 1)[1]
        stop_bot(name)
        send(chat_id, f"⏹ Stopping <b>{name}</b> — it'll finish its current ~5min check-in cycle "
                       f"gracefully and then NOT come back until you hit Resume.")

    elif data.startswith("resume_bot:"):
        name = data.split(":", 1)[1]
        resume_bot(name)
        send(chat_id, f"▶️ <b>{name}</b> resumed and starting now.")

    elif data.startswith("toggle_dryrun:"):
        name = data.split(":", 1)[1]
        send(chat_id, f"Set <b>{name}</b> to:", inline=[
            [{"text": "🧪 Dry Run", "callback_data": f"set_dryrun:{name}:true"},
             {"text": "🔴 LIVE — real money", "callback_data": f"set_dryrun:{name}:false"}],
        ])

    elif data.startswith("set_dryrun:"):
        _, name, value = data.split(":", 2)
        dry_run = (value == "true")
        set_dry_run(name, dry_run)
        mode = "Dry Run" if dry_run else "🔴 LIVE (real money)"
        send(chat_id, f"✅ {name} set to <b>{mode}</b>. Hit Restart Now (or wait for its next "
                       f"natural cycle) for this to actually take effect.")

    elif data.startswith("config:"):
        name = data.split(":", 1)[1]
        configs = gh_list_dir(PRIVATE_REPO, CONFIGS_DIR)
        buttons = [[{"text": c, "callback_data": f"setconfig:{name}:{c}"}]
                   for c in configs if c.endswith(".json")]
        buttons.append([{"text": "✏️ Type it myself", "callback_data": f"typeconfig:{name}"}])
        buttons.append([{"text": "⬆️ Upload new config file", "callback_data": f"uploadconfig:{name}"}])
        buttons.append([{"text": "🔙 Back", "callback_data": "backtolist"}])
        send(chat_id, f"Pick a base config for <b>{name}</b>:", inline=buttons)

    elif data.startswith("typeconfig:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_config_text", "bot": name}
        send(chat_id, f"Type the exact config filename for {name} (including .json, must "
                       f"match a file already in the configs folder).")

    elif data.startswith("setconfig:"):
        _, name, config_file = data.split(":", 2)
        update_bot_field(name, [("BASE_CONFIG", config_file)])
        send(chat_id, f"✅ {name} will use {config_file} on its next restart.")

    elif data.startswith("uploadconfig:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_config_upload", "bot": name}
        send(chat_id, f"Send the .json config file now for {name}.")

    elif data.startswith("restart_now:"):
        name = data.split(":", 1)[1]
        set_control_flag(name)
        send(chat_id, f"🔁 Restart requested for <b>{name}</b> — it'll stop gracefully and "
                       f"come back up within the next ~5 minutes (it only checks in on that cadence).")

    elif data.startswith("cb_stop:"):
        name = data.split(":", 1)[1]
        stop_custom_bot(name)
        send(chat_id, f"⏹ Stopping <b>{name}</b> — finishes its current cycle, then stays down "
                       f"until you hit Resume.")

    elif data.startswith("cb_resume:"):
        name = data.split(":", 1)[1]
        resume_custom_bot(name)
        send(chat_id, f"▶️ <b>{name}</b> resumed and starting now.")

    elif data.startswith("cb_delete_state:"):
        name = data.split(":", 1)[1]
        delete_custom_bot_state(name)
        send(chat_id, f"🗑 Cleared all state files for <b>{name}</b> except bot.py, "
                       f"requirements.txt, and its token. Clean slate on next restart.")

    elif data.startswith("cb_upload_py:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_cb_py_upload", "bot": name}
        send(chat_id, f"Send the new .py file for {name}.")

    elif data.startswith("cb_upload_req:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_cb_req_upload", "bot": name}
        send(chat_id, f"Send the new requirements.txt for {name}.")

    elif data.startswith("cb_delete_entirely_confirm:"):
        name = data.split(":", 1)[1]
        send(chat_id, f"⚠️ This permanently deletes custom bot <b>{name}</b> — its workflow, "
                       f"all its files, everything. Not recoverable. "
                       f"Send /confirm_delete_custom_{name} to proceed.")

    elif data == "backtolist":
        show_bot_list(chat_id)

    elif data.startswith("dd_menu:"):
        name = data.split(":", 1)[1]
        cfg = get_watchdog_config(name)
        send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))

    elif data.startswith("wd_toggle_enabled:"):
        name = data.split(":", 1)[1]
        cfg = get_watchdog_config(name)
        cfg["alerts_enabled"] = not cfg["alerts_enabled"]
        save_watchdog_config(name, cfg)
        send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))

    elif data.startswith("wd_ddalert_inc:") or data.startswith("wd_ddalert_dec:"):
        name = data.split(":", 1)[1]
        cfg = get_watchdog_config(name)
        delta = 0.5 if "inc" in data.split(":")[0] else -0.5
        cfg["drawdown_alert_pct"] = max(0.5, (cfg["drawdown_alert_pct"] or 0.5) + delta)
        save_watchdog_config(name, cfg)
        send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))

    elif data.startswith("wd_ddcap_inc:") or data.startswith("wd_ddcap_dec:"):
        name = data.split(":", 1)[1]
        cfg = get_watchdog_config(name)
        delta = 0.5 if "inc" in data.split(":")[0] else -0.5
        cfg["drawdown_cap_pct"] = max(0.5, (cfg["drawdown_cap_pct"] or 0.5) + delta)
        save_watchdog_config(name, cfg)
        send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))

    elif data.startswith("wd_peakcap_inc:") or data.startswith("wd_peakcap_dec:"):
        name = data.split(":", 1)[1]
        cfg = get_watchdog_config(name)
        delta = 0.5 if "inc" in data.split(":")[0] else -0.5
        cfg["peak_cap_pct"] = max(0.5, (cfg["peak_cap_pct"] or 0.5) + delta)
        save_watchdog_config(name, cfg)
        send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))

    elif data.startswith("wd_ddalert_direct:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_wd_value", "bot": name, "field": "drawdown_alert_pct"}
        send(chat_id, f"Type the drawdown ALERT % for {name} directly (e.g. 5), or 'off' to disable, or 'cancel'.")

    elif data.startswith("wd_ddcap_direct:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_wd_value", "bot": name, "field": "drawdown_cap_pct"}
        send(chat_id, f"Type the drawdown CAP % for {name} directly (e.g. 10) — crossing this "
                       f"auto-closes — or 'off' to disable capping, or 'cancel'.")

    elif data.startswith("wd_peakcap_direct:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_wd_value", "bot": name, "field": "peak_cap_pct"}
        send(chat_id, f"Type the peak CAP % for {name} directly (e.g. 15) — only one cap, "
                       f"crossing it auto-closes — or 'off' to disable, or 'cancel'.")

    elif data.startswith("wd_peak_edit:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_wd_peak_levels", "bot": name}
        send(chat_id, f"Send peak ALERT levels for {name} as comma-separated % gains from "
                       f"the reset point (e.g. <code>5, 10, 20</code> — fires once each as crossed), "
                       f"or 'none' to clear them.")

    elif data.startswith("wd_reset:"):
        name = data.split(":", 1)[1]
        request_hwm_reset(name)
        send(chat_id, f"♻️ Reset requested for <b>{name}</b> — its watchdog will pick this up "
                       f"on its next poll (within ~2s if it's currently running) and start "
                       f"tracking a fresh high-water mark from whatever equity it reads next.")

    elif data.startswith("bc_relay_status:"):
        name = data.split(":", 1)[1]
        path = f"freqtrade-core/user_data/live_bots/{name}/relay_status.json"
        content, _ = gh_get_file(PRIVATE_REPO, path)
        if content is None:
            send(chat_id, f"No relay status found for <b>{name}</b> yet — either it's never "
                           f"started, or hasn't reached its first status write. If {name} is "
                           f"currently running, this file should appear within a few seconds "
                           f"of startup; if it's been running a while with nothing here, the "
                           f"relay process itself likely failed to start.")
        else:
            status = json.loads(content)
            lines = [
                f"🩺 Relay status for <b>{name}</b>:",
                f"Started: <b>{status.get('started_at', '?')}</b>",
                f"Events received: <b>{status.get('events_received', 0)}</b>",
                f"Last event: <b>{status.get('last_event_type') or 'none yet'}</b>"
                + (f" at {status['last_event_at']}" if status.get("last_event_at") else ""),
            ]
            if status.get("last_error"):
                lines.append(f"Last error: <code>{status['last_error']}</code>")
            send(chat_id, "\n".join(lines))

    elif data.startswith("bc_menu:"):
        name = data.split(":", 1)[1]
        if ensure_broadcast_webhook(name):
            send(chat_id, f"ℹ️ {name}'s Freqtrade webhook config was missing or out of date — "
                           f"updated now, and a reload was requested. If {name} is currently "
                           f"running, its watchdog will apply this within ~2s with no dropped "
                           f"trades. If it's not running right now, this takes effect "
                           f"automatically the moment it next starts.")
        cfg = get_broadcast_config(name)
        send(chat_id, f"📡 Broadcast destinations for <b>{name}</b>:", inline=kb_broadcast_menu(name, cfg))

    elif data.startswith("bc_toggle_enabled:"):
        name = data.split(":", 1)[1]
        cfg = get_broadcast_config(name)
        cfg["enabled"] = not cfg["enabled"]
        save_broadcast_config(name, cfg)
        send(chat_id, f"📡 Broadcast destinations for <b>{name}</b>:", inline=kb_broadcast_menu(name, cfg))

    elif data.startswith("bc_toggle_followable:"):
        name = data.split(":", 1)[1]
        cfg = get_broadcast_config(name)
        cfg["followable"] = not cfg.get("followable")
        save_broadcast_config(name, cfg)
        note = (f"<b>{name}</b> will now appear in the automate-bot's follow picker, once "
                f"that's built." if cfg["followable"] else
                f"<b>{name}</b> removed from the follow picker. Anyone already following it "
                f"keeps following it until you pause or remove them — this only affects new "
                f"followers choosing a bot.")
        send(chat_id, note, inline=kb_broadcast_menu(name, cfg))

    elif data.startswith("bc_stagger_edit:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_bc_stagger", "bot": name}
        send(chat_id, f"Type the stagger delay in seconds between Telegram sends for {name} "
                       f"(e.g. 1, or 0.5, or 0 for none). This only applies to Telegram "
                       f"destinations with stagger turned on — webhook destinations always "
                       f"fire immediately in parallel.")

    elif data.startswith("bc_dest_new:"):
        name = data.split(":", 1)[1]
        send(chat_id, f"Add a destination for <b>{name}</b>:", inline=[
            [{"text": "📡 Telegram chat/channel", "callback_data": f"bc_dest_new_type:{name}:telegram"},
             {"text": "🔗 Webhook URL", "callback_data": f"bc_dest_new_type:{name}:webhook"}],
            [{"text": "🔙 Back", "callback_data": f"bc_menu:{name}"}],
        ])

    elif data.startswith("bc_dest_new_type:"):
        _, name, dtype = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_new_dest", "bot": name, "dtype": dtype}
        if dtype == "telegram":
            send(chat_id, f"Send the chat ID(s) for {name}'s new Telegram destination. "
                          f"You can add several at once separated by <code>+</code>, e.g.\n"
                          f"<code>-1001234567890+-1009876543210</code>\n"
                          f"Each becomes its own destination (own scale/filters/pause).")
        else:
            send(chat_id, f"Send the webhook URL(s) for {name}'s new destination. "
                          f"Separate multiple with <code>+</code>. Each will receive the same "
                          f"JSON payload Freqtrade itself produces (plus a \"type\" field), "
                          f"filtered per that destination's settings.")

    elif data.startswith("bc_dest_open:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"].get(did)
        if not dest:
            send(chat_id, "That destination no longer exists.")
        else:
            send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
                 inline=kb_broadcast_dest_menu(name, did, dest))

    elif data.startswith("bc_dest_toggleactive:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"][did]
        dest["active"] = not dest["active"]
        save_broadcast_config(name, cfg)
        send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
             inline=kb_broadcast_dest_menu(name, did, dest))

    elif data.startswith("bc_dest_togglestagger:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"][did]
        dest["stagger"] = not dest["stagger"]
        save_broadcast_config(name, cfg)
        send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
             inline=kb_broadcast_dest_menu(name, did, dest))

    elif data.startswith("bc_dest_rename:"):
        _, name, did = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_rename", "bot": name, "dest": did}
        send(chat_id, f"Type the new label for this destination.")

    elif data.startswith("bc_dest_scale:"):
        _, name, did = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_scale", "bot": name, "dest": did}
        send(chat_id, f"Type the scale multiplier for this destination relative to your "
                       f"account (e.g. 1, 2, 0.3, 0.03). This only scales the displayed stake "
                       f"amount — percentage returns are identical at any scale.")

    elif data.startswith("bc_dest_editurl:"):
        _, name, did = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_editurl", "bot": name, "dest": did}
        send(chat_id, f"Send the new webhook URL for this destination.")

    elif data.startswith("bc_dest_editchatid:"):
        _, name, did = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_editchatid", "bot": name, "dest": did}
        send(chat_id, f"Send the new chat ID for this destination.")

    elif data.startswith("bc_field_toggle:"):
        _, name, did, field = data.split(":", 3)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"][did]
        dest["fields"][field] = not dest["fields"][field]
        save_broadcast_config(name, cfg)
        send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
             inline=kb_broadcast_dest_menu(name, did, dest))

    elif data.startswith("bc_dest_preview:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"][did]
        entry_msg = render_broadcast_message("entry_fill", BROADCAST_SAMPLE_TRADE["entry_fill"],
                                               dest["fields"], dest["scale"])
        exit_msg = render_broadcast_message("exit_fill", BROADCAST_SAMPLE_TRADE["exit_fill"],
                                              dest["fields"], dest["scale"])
        preview_text = "<i>This is a preview using a sample trade — nothing was sent.</i>\n\n"
        preview_text += (entry_msg or "<i>(entry_fill is off for this destination)</i>") + "\n\n"
        preview_text += (exit_msg or "<i>(exit_fill is off for this destination)</i>")
        send(chat_id, preview_text, inline=kb_broadcast_dest_menu(name, did, dest))

    elif data.startswith("bc_dest_test:"):
        _, name, did = data.split(":", 2)
        state["pending"][chat_id] = {"action": "await_bc_test_text", "bot": name, "dest": did}
        send(chat_id, f"Type a message to send to this destination right now, to confirm "
                       f"it's reachable (e.g. \"ping\"). This is a real send, just with text "
                       f"you choose instead of a real trade.")

    elif data.startswith("bc_dest_delete_confirm:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        dest = cfg["destinations"].get(did)
        label = dest["label"] if dest else did
        send(chat_id, f"Remove destination <b>{label}</b> from {name}'s broadcast list? "
                       f"This only removes it from broadcasting — it doesn't affect the bot itself.",
             inline=[[{"text": "🗑 Yes, remove it", "callback_data": f"bc_dest_delete:{name}:{did}"},
                       {"text": "Cancel", "callback_data": f"bc_dest_open:{name}:{did}"}]])

    elif data.startswith("bc_dest_delete:"):
        _, name, did = data.split(":", 2)
        cfg = get_broadcast_config(name)
        cfg["destinations"].pop(did, None)
        save_broadcast_config(name, cfg)
        send(chat_id, f"📡 Broadcast destinations for <b>{name}</b>:", inline=kb_broadcast_menu(name, cfg))

    elif data.startswith("delete_entirely_confirm:"):
        name = data.split(":", 1)[1]
        send(chat_id, f"⚠️ This permanently deletes <b>{name}</b> — its workflow, DB, secrets, "
                       f"everything. Not recoverable. Send /confirm_delete_entirely_{name} to proceed.")

    elif data.startswith("sched_menu:"):
        name = data.split(":", 1)[1]
        sched = get_bot_schedule(state, name)
        send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b> (times are Lagos local time):",
             inline=kb_schedule_menu(name, sched))

    elif data.startswith("sched_delete_toggle:"):
        name = data.split(":", 1)[1]
        sched = get_bot_schedule(state, name)
        sched["delete_db"]["enabled"] = not sched["delete_db"]["enabled"]
        save_state(state)
        send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b>:", inline=kb_schedule_menu(name, sched))

    elif data.startswith("sched_reload_toggle:"):
        name = data.split(":", 1)[1]
        sched = get_bot_schedule(state, name)
        sched["reload_config"]["enabled"] = not sched["reload_config"]["enabled"]
        save_state(state)
        send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b>:", inline=kb_schedule_menu(name, sched))

    elif data.startswith("sched_delete_edit:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_times", "bot": name, "task": "delete_db"}
        send(chat_id, f"Send delete-DB times for {name} as comma-separated HH:MM (Lagos time), "
                      f"e.g. <code>02:00, 14:00</code> — or send <code>none</code> to clear them.")

    elif data.startswith("sched_reload_edit:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_times", "bot": name, "task": "reload_config"}
        send(chat_id, f"Send reload-config times for {name} as comma-separated HH:MM (Lagos time), "
                      f"e.g. <code>06:00</code> — or send <code>none</code> to clear them.")

    elif data.startswith("sched_hyperopt_toggle:"):
        name = data.split(":", 1)[1]
        sched = get_bot_schedule(state, name)
        sched["hyperopt"]["enabled"] = not sched["hyperopt"]["enabled"]
        save_state(state)
        send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b>:", inline=kb_schedule_menu(name, sched))

    elif data.startswith("sched_hyperopt_edit:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_times", "bot": name, "task": "hyperopt"}
        send(chat_id, f"Send hyperopt run times for {name} as comma-separated HH:MM (Lagos time) "
                      f"— this can take hours, pick times accordingly — or <code>none</code> to clear.")

    elif data.startswith("hp_loss:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "loss_function"}
        send(chat_id, f"Type the hyperopt loss function class name for {name} "
                      f"(e.g. AbyssalMinimaxLoss, SharpeHyperOptLoss).")

    elif data.startswith("hp_lookback:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "lookback_days"}
        send(chat_id, f"Type lookback window in days for {name} (e.g. 90).")

    elif data.startswith("hp_epochs:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "epochs"}
        send(chat_id, f"Type epoch count for {name} (e.g. 500).")

    elif data.startswith("hp_earlystop:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "early_stop"}
        send(chat_id, f"Type early-stop epoch count for {name} (stops if no improvement for this "
                      f"many epochs), or 0 to disable.")

    elif data.startswith("hp_seed:"):
        name = data.split(":", 1)[1]
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "random_state"}
        send(chat_id, f"Type the random seed/state for {name} (any integer — same seed reproduces "
                      f"the same search).")

    elif data.startswith("hp_startmode:"):
        name = data.split(":", 1)[1]
        cfg = get_hyperopt_config(name)
        order = ["warm", "cold", "upload"]
        current = order.index(cfg["warm_start_mode"]) if cfg["warm_start_mode"] in order else 0
        cfg["warm_start_mode"] = order[(current + 1) % len(order)]
        save_hyperopt_config(name, cfg)
        if cfg["warm_start_mode"] == "upload":
            state["pending"][chat_id] = {"action": "await_hp_warmstart_upload", "bot": name}
            send(chat_id, f"Mode set to upload — send the warm-start JSON file for {name} now "
                          f"(either a raw hyperopt epoch result or an already-formatted params file).")
        else:
            send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b>:",
                 inline=kb_schedule_menu(name, get_bot_schedule(state, name)))

    elif data.startswith("hp_spaces:"):
        name = data.split(":", 1)[1]
        cfg = get_hyperopt_config(name)
        current = cfg.get("spaces", "all")
        state["pending"][chat_id] = {"action": "await_hp_field", "bot": name, "field": "spaces"}
        send(chat_id,
             f"Type the spaces to optimize for <b>{name}</b>, separated by spaces.\n"
             f"Valid values: <code>all default buy sell roi stoploss trailing</code>\n"
             f"Examples: <code>all</code> / <code>buy sell</code> / <code>roi stoploss</code>\n"
             f"Current: <code>{current}</code>")

    elif data.startswith("hp_reloadafter_toggle:"):
        name = data.split(":", 1)[1]
        cfg = get_hyperopt_config(name)
        cfg["reload_after"] = not cfg["reload_after"]
        save_hyperopt_config(name, cfg)
        send(chat_id, f"⏰ Scheduled tasks for <b>{name}</b>:",
             inline=kb_schedule_menu(name, get_bot_schedule(state, name)))

    elif data.startswith("hp_run_now:"):
        name = data.split(":", 1)[1]
        trigger_hyperopt(name)
        send(chat_id, f"🚀 Hyperopt running now for <b>{name}</b> — this is a separate, "
                      f"one-shot job and won't interrupt the live bot. Can take a while; "
                      f"you'll get a Telegram message through {name}'s own bot when it's done.")


def prompt_dry_run_choice(chat_id):
    send(chat_id, "Dry run or live for this bot?", inline=[
        [{"text": "🧪 Dry Run", "callback_data": "newbot_dryrun:true"},
         {"text": "🔴 LIVE — real money", "callback_data": "newbot_dryrun:false"}],
    ])


def handle_custom_bot_flow(state, chat_id, msg):
    pending = state["pending"].get(chat_id, {})
    step = pending.get("custom_bot_step")
    text = msg.get("text", "")

    if step is None:
        state["pending"][chat_id] = {"custom_bot_step": "name"}
        send(chat_id, "Custom bot — what should it be called? (used in filenames, no spaces)", reply=False)
        return

    if step == "name":
        pending["name"] = text.strip()
        pending["custom_bot_step"] = "token"
        send(chat_id, "Send the BotFather token for this bot — same idea as the Freqtrade bots, "
                      "your script can use this however it implements its own Telegram interface.",
             reply=False)

    elif step == "token":
        pending["token"] = text.strip()
        pending["custom_bot_step"] = "await_py"
        send(chat_id, "Now send the .py file — this is the whole bot, run directly, not a "
                      "Freqtrade strategy plugin.", reply=False)

    elif step == "await_py":
        send(chat_id, "Send the .py file as a document, not text.", reply=False)

    elif step == "await_requirements":
        # FIX: accept either 'none' as text, or a typed requirements body as
        # plain text (e.g. pasted inline). Document upload is handled in
        # handle_document; this branch only sees plain text messages.
        stripped = text.strip()
        if stripped.lower() == "none":
            requirements_bytes = b"# no extra dependencies\n"
        else:
            # Treat whatever the user typed as the requirements content directly.
            # This lets them paste e.g. "requests\nnumpy" without uploading a file.
            requirements_bytes = stripped.encode()

        # py_content was committed to GitHub immediately when the file arrived
        # (see handle_document) — only the filename is in pending now.
        py_content_bytes, _ = gh_get_file(
            PRIVATE_REPO,
            f"freqtrade-core/user_data/custom_bots/{pending['name']}/bot.py"
        )
        create_custom_bot(pending["name"], pending["token"],
                          py_content_bytes, requirements_bytes)
        send(chat_id, f"✅ Custom bot <b>{pending['name']}</b> created and starting now.")
        del state["pending"][chat_id]
        return

    state["pending"][chat_id] = pending


def handle_new_bot_flow(state, chat_id, msg):
    pending = state["pending"].get(chat_id, {})
    step = pending.get("step")
    text = msg.get("text", "")

    if step is None:
        state["pending"][chat_id] = {"step": "name"}
        send(chat_id, "New bot — what should it be called? (used in filenames, no spaces)", reply=False)
        return

    if step == "name":
        pending["name"] = text.strip()
        pending["step"] = "token"
        send(chat_id, "Send the BotFather token for this bot.", reply=False)

    elif step == "token":
        pending["token"] = text.strip()
        pending["step"] = "config"
        configs = gh_list_dir(PRIVATE_REPO, CONFIGS_DIR)
        buttons = [[{"text": c, "callback_data": f"newbot_config:{c}"}] for c in configs if c.endswith(".json")]
        buttons.append([{"text": "✏️ Type it myself", "callback_data": "newbot_config_text"}])
        buttons.append([{"text": "⬆️ Upload new config file", "callback_data": "newbot_config_upload"}])
        send(chat_id, "Pick a base config:", inline=buttons)
        return  # stays on this step until callback below fires

    elif step == "await_config_text_newbot":
        pending["config"] = text.strip()
        pending["step"] = "strategy"
        strategies = gh_list_dir(PRIVATE_REPO, STRATEGIES_DIR)
        buttons = [[{"text": s, "callback_data": f"newbot_strategy:{s}"}] for s in strategies if s.endswith(".py")]
        buttons.append([{"text": "✏️ Type it myself", "callback_data": "newbot_strategy_text"}])
        buttons.append([{"text": "⬆️ Upload new strategy file", "callback_data": "newbot_strategy_upload"}])
        send(chat_id, "Pick a strategy:", inline=buttons)
        return

    elif step == "await_strategy_text_newbot":
        pending["strategy"] = text.strip()
        pending["step"] = "dry_run_choice"
        prompt_dry_run_choice(chat_id)
        return

    elif step == "api_key":
        pending["api_key"] = text.strip()
        pending["step"] = "api_secret"
        send(chat_id, "Send the exchange API secret for this bot's sub-account.", reply=False)

    elif step == "api_secret":
        pending["api_secret"] = text.strip()
        create_bot(pending["name"], pending["token"], pending["config"],
                   pending["strategy"], pending["dry_run"], pending["api_key"], pending["api_secret"])
        mode = "Dry Run" if pending["dry_run"] else "🔴 LIVE"
        send(chat_id, f"✅ {pending['name']} created in <b>{mode}</b> mode and starting now.", reply=False)
        pending["step"] = "post_create_drawdown_choice"
        send(chat_id, "Set a drawdown alert/cap now for this bot?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:drawdown"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:drawdown"}],
        ])
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_dd_alert":
        if text.strip().lower() == "off":
            pending["_dd_alert"] = None
        else:
            try:
                pending["_dd_alert"] = float(text.strip().replace("%", ""))
            except ValueError:
                send(chat_id, "Not a number — try again, e.g. 5, or 'off'.", reply=False)
                return
        pending["step"] = "post_create_dd_cap"
        send(chat_id, "Drawdown CAP % (auto-closes) — or 'off':", reply=False)
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_dd_cap":
        if text.strip().lower() == "off":
            cap = None
        else:
            try:
                cap = float(text.strip().replace("%", ""))
            except ValueError:
                send(chat_id, "Not a number — try again, e.g. 10, or 'off'.", reply=False)
                return
        cfg = get_watchdog_config(pending["name"])
        cfg["drawdown_alert_pct"] = pending.pop("_dd_alert", None)
        cfg["drawdown_cap_pct"] = cap
        cfg["alerts_enabled"] = True
        save_watchdog_config(pending["name"], cfg)
        send(chat_id, "✅ Drawdown set.", reply=False)
        pending["step"] = "post_create_peak_choice"
        send(chat_id, "Set peak alerts/cap now for this bot?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:peak"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:peak"}],
        ])
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_peak_levels":
        if text.strip().lower() == "none":
            pending["_peak_levels"] = []
        else:
            try:
                pending["_peak_levels"] = sorted(float(p.strip().replace("%", "")) for p in text.split(","))
            except ValueError:
                send(chat_id, "Couldn't parse that — comma-separated %, e.g. 5, 10, 20, or 'none'.", reply=False)
                return
        pending["step"] = "post_create_peak_cap"
        send(chat_id, "Peak CAP % (auto-closes, only one) — or 'off':", reply=False)
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_peak_cap":
        if text.strip().lower() == "off":
            cap = None
        else:
            try:
                cap = float(text.strip().replace("%", ""))
            except ValueError:
                send(chat_id, "Not a number — try again, e.g. 15, or 'off'.", reply=False)
                return
        cfg = get_watchdog_config(pending["name"])
        cfg["peak_alert_levels"] = pending.pop("_peak_levels", [])
        cfg["peak_cap_pct"] = cap
        cfg["alerts_enabled"] = True
        save_watchdog_config(pending["name"], cfg)
        send(chat_id, "✅ Peak settings set.", reply=False)
        pending["step"] = "post_create_dbdelete_choice"
        send(chat_id, "Schedule auto DB-delete for this bot now?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:dbdelete"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:dbdelete"}],
        ])
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_dbdelete_times":
        sched = get_bot_schedule(state, pending["name"])
        if text.strip().lower() != "none":
            sched["delete_db"]["times"] = [t.strip() for t in text.split(",")]
            sched["delete_db"]["enabled"] = True
        send(chat_id, "✅ DB-delete schedule set.", reply=False)
        pending["step"] = "post_create_reload_choice"
        send(chat_id, "Schedule auto reload-config for this bot now?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:reload"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:reload"}],
        ])
        state["pending"][chat_id] = pending
        return

    elif step == "post_create_reload_times":
        sched = get_bot_schedule(state, pending["name"])
        if text.strip().lower() != "none":
            sched["reload_config"]["times"] = [t.strip() for t in text.split(",")]
            sched["reload_config"]["enabled"] = True
        send(chat_id, f"✅ {pending['name']} is fully set up. Use 📋 List Bots any time to "
                       f"reconfigure anything — none of this was your only chance at it.")
        del state["pending"][chat_id]
        return

    state["pending"][chat_id] = pending


def handle_new_bot_callback(state, cq):
    chat_id = str(cq["message"]["chat"]["id"])
    data = cq["data"]
    pending = state["pending"].get(chat_id, {})
    answer_callback(cq["id"])

    if data == "postcreate_set:drawdown":
        pending["step"] = "post_create_dd_alert"
        send(chat_id, "Drawdown ALERT % (just notifies) — or 'off':", reply=False)
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_skip:drawdown":
        pending["step"] = "post_create_peak_choice"
        send(chat_id, "Set peak alerts/cap now for this bot?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:peak"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:peak"}],
        ])
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_set:peak":
        pending["step"] = "post_create_peak_levels"
        send(chat_id, "Peak ALERT levels, comma-separated % gains (e.g. 5, 10, 20), or 'none':", reply=False)
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_skip:peak":
        pending["step"] = "post_create_dbdelete_choice"
        send(chat_id, "Schedule auto DB-delete for this bot now?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:dbdelete"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:dbdelete"}],
        ])
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_set:dbdelete":
        pending["step"] = "post_create_dbdelete_times"
        send(chat_id, "DB-delete times, comma-separated HH:MM (Lagos time), or 'none':", reply=False)
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_skip:dbdelete":
        pending["step"] = "post_create_reload_choice"
        send(chat_id, "Schedule auto reload-config for this bot now?", inline=[
            [{"text": "✅ Set now", "callback_data": "postcreate_set:reload"},
             {"text": "⏭ Skip — set later", "callback_data": "postcreate_skip:reload"}],
        ])
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_set:reload":
        pending["step"] = "post_create_reload_times"
        send(chat_id, "Reload-config times, comma-separated HH:MM (Lagos time), or 'none':", reply=False)
        state["pending"][chat_id] = pending
        return

    if data == "postcreate_skip:reload":
        name = pending.get("name", "Bot")
        send(chat_id, f"✅ {name} is fully set up. Use 📋 List Bots any time to reconfigure "
                       f"anything — none of this was your only chance at it.")
        del state["pending"][chat_id]
        return

    if data.startswith("newbot_config:"):
        pending["config"] = data.split(":", 1)[1]
        pending["step"] = "strategy"
        strategies = gh_list_dir(PRIVATE_REPO, STRATEGIES_DIR)
        buttons = [[{"text": s, "callback_data": f"newbot_strategy:{s}"}] for s in strategies if s.endswith(".py")]
        buttons.append([{"text": "✏️ Type it myself", "callback_data": "newbot_strategy_text"}])
        buttons.append([{"text": "⬆️ Upload new strategy file", "callback_data": "newbot_strategy_upload"}])
        send(chat_id, "Pick a strategy:", inline=buttons)

    elif data == "newbot_config_text":
        pending["step"] = "await_config_text_newbot"
        send(chat_id, "Type the exact config filename, including .json.", reply=False)

    elif data == "newbot_config_upload":
        pending["step"] = "await_config_upload_newbot"
        send(chat_id, "Send the .json config file now.", reply=False)

    elif data.startswith("newbot_strategy:"):
        pending["strategy"] = data.split(":", 1)[1][:-3]
        pending["step"] = "dry_run_choice"
        prompt_dry_run_choice(chat_id)

    elif data == "newbot_strategy_text":
        pending["step"] = "await_strategy_text_newbot"
        send(chat_id, "Type the exact strategy class name, no .py.", reply=False)

    elif data == "newbot_strategy_upload":
        pending["step"] = "await_strategy_upload_newbot"
        send(chat_id, "Send the .py strategy file now.", reply=False)

    elif data.startswith("newbot_dryrun:"):
        dry_run = data.split(":", 1)[1] == "true"
        pending["dry_run"] = dry_run
        pending["step"] = "api_key"
        mode_note = ("Dry run is ON — no real orders will be placed, but Freqtrade still "
                     "needs exchange API credentials for market data and balance checks."
                     if dry_run else
                     "🔴 LIVE mode — this bot WILL place real orders with real funds.")
        send(chat_id, f"{mode_note}\n\nSend the exchange API key for this bot's sub-account.", reply=False)

    state["pending"][chat_id] = pending


def handle_strategy_text_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name = pending["bot"]
    update_bot_field(name, [("STRATEGY_CLASS", text.strip())])
    del state["pending"][chat_id]
    send(chat_id, f"✅ {name} will use {text.strip()} on its next restart.")


def handle_config_text_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name = pending["bot"]
    update_bot_field(name, [("BASE_CONFIG", text.strip())])
    del state["pending"][chat_id]
    send(chat_id, f"✅ {name} will use {text.strip()} on its next restart.")


def handle_hp_field_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, field = pending["bot"], pending["field"]
    cfg = get_hyperopt_config(name)
    text = text.strip()

    VALID_SPACES = {"all", "default", "buy", "sell", "roi", "stoploss", "trailing"}

    if field == "loss_function":
        cfg[field] = text
    elif field == "spaces":
        tokens = text.lower().split()
        invalid = [t for t in tokens if t not in VALID_SPACES]
        if not tokens:
            send(chat_id, "Can't be empty — enter at least one space name.")
            return
        if invalid:
            send(chat_id,
                 f"❌ Unknown space(s): <code>{' '.join(invalid)}</code>\n"
                 f"Valid: <code>all default buy sell roi stoploss trailing</code>\nTry again.")
            return
        cfg[field] = " ".join(tokens)
    else:
        try:
            cfg[field] = int(text)
        except ValueError:
            send(chat_id, "That's not a whole number — try again.")
            return

    save_hyperopt_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"✅ Updated. Schedule for <b>{name}</b>:",
         inline=kb_schedule_menu(name, get_bot_schedule(state, name)))


def handle_wd_value_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, field = pending["bot"], pending["field"]
    text_clean = text.strip().lower()
    cfg = get_watchdog_config(name)
    if text_clean == "cancel":
        del state["pending"][chat_id]
        send(chat_id, "Cancelled.")
        return
    if text_clean == "off":
        cfg[field] = None
    else:
        try:
            cfg[field] = max(0.1, float(text.strip().replace("%", "")))
        except ValueError:
            send(chat_id, "That's not a number — try again, e.g. 7.5, 'off', or 'cancel'.")
            return
    save_watchdog_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))


def handle_wd_peak_levels_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name = pending["bot"]
    cfg = get_watchdog_config(name)
    if text.strip().lower() == "none":
        cfg["peak_alert_levels"] = []
    else:
        levels = []
        for part in text.split(","):
            try:
                levels.append(float(part.strip().replace("%", "")))
            except ValueError:
                pass
        if not levels:
            send(chat_id, "Couldn't parse that — comma-separated %, e.g. 5, 10, 20")
            return
        cfg["peak_alert_levels"] = sorted(levels)
    save_watchdog_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"📉 Drawdown control for <b>{name}</b>:", inline=kb_drawdown_menu(name, cfg))


def build_webhook_payload(bot_name, event_type, data, dest_fields, scale=1.0):
    """The structured (non-Telegram) payload sent to webhook destinations —
    e.g. the automate/copy-trading bot — as actual fields rather than the
    rendered HTML text meant for human reading in Telegram. Same
    field-filtering and stake scaling as render_broadcast_message, applied
    to real data instead of formatted text, so a machine consumer gets
    clean fields it can act on (pair, trade_id, rates) without having to
    parse anything back out of a message string.

    `bot_name` is always included as "source_bot", unconditionally and
    not gated by any field toggle — it's routing information, not optional
    trade detail. A webhook consumer that aggregates events from several
    of your bots at once (which the automate-bot does by design — each
    follower can choose any one of your followable bots) has no other way
    to know which bot a given event came from, since every followable bot
    posts to the SAME automate-bot URL. Without this field, automate-bot
    would have no way to match an incoming trade to the followers who
    should mirror it.

    Returns None if this event type is off for this destination, same as
    render_broadcast_message — callers should skip sending in that case.
    """
    event_gate = {
        "entry": "event_entry_fill", "entry_fill": "event_entry_fill",
        "entry_cancel": "event_entry_cancel",
        "exit": "event_exit_fill", "exit_fill": "event_exit_fill",
        "exit_cancel": "event_exit_cancel",
        "status": "event_status", "reload": "event_reload",
    }.get(event_type)
    if event_gate and not dest_fields.get(event_gate, True):
        return None

    if event_type in ("status", "reload"):
        payload = dict(data)
        payload["source_bot"] = bot_name
        return payload

    payload = {
        "source_bot": bot_name,
        "type": event_type,
        "pair": data.get("pair"),
        "direction": data.get("direction"),
        "trade_id": data.get("trade_id"),
    }
    if event_type in ("entry", "entry_fill", "exit", "exit_fill"):
        if dest_fields.get("show_leverage", True):
            payload["leverage"] = data.get("leverage")
        if dest_fields.get("show_fills", True):
            if data.get("open_rate") is not None:
                payload["open_rate"] = data["open_rate"]
            if data.get("close_rate") is not None:
                payload["close_rate"] = data["close_rate"]
        if dest_fields.get("show_stake", False) and data.get("stake_amount") is not None:
            payload["stake_amount"] = float(data["stake_amount"]) * scale
            payload["scale"] = scale
        if dest_fields.get("show_tags", True):
            if data.get("enter_tag"):
                payload["enter_tag"] = data["enter_tag"]
            if data.get("exit_reason"):
                payload["exit_reason"] = data["exit_reason"]
    if event_type in ("exit", "exit_fill"):
        payload["profit_ratio"] = data.get("profit_ratio")
        if data.get("close_date"):
            payload["close_date"] = data["close_date"]

    return payload


def send_to_destination(dest, text, payload=None):
    """One real send, to one destination, right now. Used by test-send here
    in the meta-bot, and by the relay subprocess for real trade events —
    kept here as the single source of truth for HOW a destination is
    reached, so test-send genuinely proves the live path works.

    `text` (rendered HTML) is what Telegram destinations receive.
    `payload` (a plain dict of real fields, from build_webhook_payload) is
    what webhook destinations receive — a machine consumer needs actual
    fields, not an HTML string to parse back apart. If `payload` isn't
    given (the test-send case, where there's no real trade to draw from),
    webhook destinations fall back to a minimal {"type": "test", "text":
    ...} envelope, which is fine since test-send only needs to prove
    reachability, not mimic a real event's shape.

    Returns (ok: bool, detail: str)."""
    if dest["type"] == "telegram":
        token = dest.get("bot_token") or META_BOT_TOKEN
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                               json={"chat_id": dest["chat_id"], "text": text, "parse_mode": "HTML"},
                               timeout=10)
            if r.status_code == 200:
                return True, "sent"
            return False, f"Telegram HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)
    else:  # webhook
        body = payload if payload is not None else {"type": "test", "text": text}
        try:
            r = requests.post(dest["url"], json=body, timeout=10)
            return (200 <= r.status_code < 300), f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)


def handle_bc_stagger_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name = pending["bot"]
    cfg = get_broadcast_config(name)
    try:
        cfg["stagger_seconds"] = max(0.0, float(text.strip()))
    except ValueError:
        send(chat_id, "That's not a number — try e.g. 1, 0.5, or 0.")
        return
    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"📡 Broadcast destinations for <b>{name}</b>:", inline=kb_broadcast_menu(name, cfg))


def handle_bc_new_dest_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, dtype = pending["bot"], pending["dtype"]
    cfg = get_broadcast_config(name)
    raw_values = [v.strip() for v in text.split("+") if v.strip()]
    if not raw_values:
        send(chat_id, "Didn't catch anything usable — try again.")
        return

    added = []
    for value in raw_values:
        did = new_destination_id(cfg)
        dest = {
            "type": dtype,
            "label": value if dtype == "webhook" else f"Chat {value}",
            "active": True,
            "scale": 1.0,
            "stagger": (dtype == "telegram"),
            "fields": dict(BROADCAST_DEFAULT_FIELDS),
        }
        if dtype == "telegram":
            dest["chat_id"] = value
        else:
            dest["url"] = value
        cfg["destinations"][did] = dest
        added.append(value)

    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"✅ Added {len(added)} destination(s) to <b>{name}</b>: {', '.join(added)}\n\n"
                   f"Each starts active, scale 1x, conservative field defaults (no stake amount "
                   f"shown). Open it from the list below to adjust, preview, or test it.",
         inline=kb_broadcast_menu(name, cfg))


def handle_bc_rename_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, did = pending["bot"], pending["dest"]
    cfg = get_broadcast_config(name)
    dest = cfg["destinations"].get(did)
    if not dest:
        send(chat_id, "That destination no longer exists.")
        del state["pending"][chat_id]
        return
    dest["label"] = text.strip()[:60]
    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
         inline=kb_broadcast_dest_menu(name, did, dest))


def handle_bc_scale_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, did = pending["bot"], pending["dest"]
    cfg = get_broadcast_config(name)
    dest = cfg["destinations"].get(did)
    if not dest:
        send(chat_id, "That destination no longer exists.")
        del state["pending"][chat_id]
        return
    try:
        scale = float(text.strip().lower().rstrip("x"))
        if scale <= 0:
            raise ValueError
    except ValueError:
        send(chat_id, "That's not a usable scale — try e.g. 1, 2, 0.3, 0.03.")
        return
    dest["scale"] = scale
    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
         inline=kb_broadcast_dest_menu(name, did, dest))


def handle_bc_editurl_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, did = pending["bot"], pending["dest"]
    cfg = get_broadcast_config(name)
    dest = cfg["destinations"].get(did)
    if not dest:
        send(chat_id, "That destination no longer exists.")
        del state["pending"][chat_id]
        return
    dest["url"] = text.strip()
    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
         inline=kb_broadcast_dest_menu(name, did, dest))


def handle_bc_editchatid_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, did = pending["bot"], pending["dest"]
    cfg = get_broadcast_config(name)
    dest = cfg["destinations"].get(did)
    if not dest:
        send(chat_id, "That destination no longer exists.")
        del state["pending"][chat_id]
        return
    dest["chat_id"] = text.strip()
    save_broadcast_config(name, cfg)
    del state["pending"][chat_id]
    send(chat_id, f"Destination <b>{dest['label']}</b> for <b>{name}</b>:",
         inline=kb_broadcast_dest_menu(name, did, dest))


def handle_bc_test_text_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, did = pending["bot"], pending["dest"]
    cfg = get_broadcast_config(name)
    dest = cfg["destinations"].get(did)
    del state["pending"][chat_id]
    if not dest:
        send(chat_id, "That destination no longer exists.")
        return
    ok, detail = send_to_destination(dest, f"🧪 <b>Test message</b> from {name}:\n\n{text}")
    if ok:
        send(chat_id, f"✅ Sent to <b>{dest['label']}</b>.", inline=kb_broadcast_dest_menu(name, did, dest))
    else:
        send(chat_id, f"❌ Failed to reach <b>{dest['label']}</b>: {detail}",
             inline=kb_broadcast_dest_menu(name, did, dest))


def handle_times_input(state, chat_id, text):
    pending = state["pending"][chat_id]
    name, task = pending["bot"], pending["task"]
    sched = get_bot_schedule(state, name)

    if text.strip().lower() == "none":
        sched[task]["times"] = []
        sched[task]["last_run"] = {}
    else:
        times = []
        for part in text.split(","):
            part = part.strip()
            if len(part) == 5 and part[2] == ":" and part[:2].isdigit() and part[3:].isdigit():
                times.append(part)
        if not times:
            send(chat_id, "Couldn't parse that — use HH:MM, comma-separated, e.g. 02:00, 14:00")
            return
        sched[task]["times"] = times

    del state["pending"][chat_id]
    save_state(state)
    send(chat_id, f"⏰ Updated. Schedule for <b>{name}</b>:", inline=kb_schedule_menu(name, sched))


def handle_document(state, chat_id, msg):
    pending = state["pending"].get(chat_id, {})
    action = pending.get("action")
    step = pending.get("step")
    custom_step = pending.get("custom_bot_step")
    doc = msg["document"]
    content = download_telegram_file(doc["file_id"])

    # FIX: immediately commit the .py to GitHub and store only the bot name
    # in pending — never store raw bytes in state, which crashes json.dumps.
    if custom_step == "await_py":
        if not doc["file_name"].endswith(".py"):
            send(chat_id, "That doesn't look like a .py file — send the bot's Python file.")
            return
        bot_name = pending["name"]
        gh_put_file(PRIVATE_REPO,
                    f"freqtrade-core/user_data/custom_bots/{bot_name}/bot.py",
                    content, f"meta-bot: upload bot.py for custom bot {bot_name}")
        pending["custom_bot_step"] = "await_requirements"
        state["pending"][chat_id] = pending
        save_state(state)  # save now — offset already advanced, bytes not in state
        send(chat_id, "Got it. Now send the requirements.txt for this bot — even if it's just "
                      "one line, or 'none' as plain text if it needs nothing beyond the standard "
                      "library.", reply=False)
        return

    # FIX: same pattern — commit immediately, then call create_custom_bot
    # which re-reads bot.py from GitHub rather than carrying bytes forward.
    if custom_step == "await_requirements":
        bot_name = pending["name"]
        gh_put_file(PRIVATE_REPO,
                    f"freqtrade-core/user_data/custom_bots/{bot_name}/requirements.txt",
                    content, f"meta-bot: upload requirements.txt for custom bot {bot_name}")
        py_bytes, _ = gh_get_file(
            PRIVATE_REPO,
            f"freqtrade-core/user_data/custom_bots/{bot_name}/bot.py"
        )
        create_custom_bot(bot_name, pending["token"], py_bytes, content)
        send(chat_id, f"✅ Custom bot <b>{bot_name}</b> created and starting now.")
        del state["pending"][chat_id]
        return

    if action == "await_cb_py_upload":
        name = pending["bot"]
        gh_put_file(PRIVATE_REPO, f"freqtrade-core/user_data/custom_bots/{name}/bot.py",
                    content, f"meta-bot: update bot.py for custom bot {name}")
        del state["pending"][chat_id]
        send(chat_id, f"✅ Updated bot.py for {name} — takes effect on its next restart.")
        return

    if action == "await_cb_req_upload":
        name = pending["bot"]
        gh_put_file(PRIVATE_REPO, f"freqtrade-core/user_data/custom_bots/{name}/requirements.txt",
                    content, f"meta-bot: update requirements.txt for custom bot {name}")
        del state["pending"][chat_id]
        send(chat_id, f"✅ Updated requirements.txt for {name} — takes effect on its next restart.")
        return

    if action == "await_hp_warmstart_upload":
        name = pending["bot"]
        path = f"freqtrade-core/user_data/live_bots/{name}/hyperopt_warmstart_upload.json"
        gh_put_file(PRIVATE_REPO, path, content, f"meta-bot: hyperopt warm-start upload for {name}")
        del state["pending"][chat_id]
        send(chat_id, f"✅ Warm-start file saved for {name}. It'll be normalized and used "
                      f"automatically next time hyperopt runs for this bot.")
        return

    if action in ("await_strategy_upload", "await_config_upload"):
        target_dir = STRATEGIES_DIR if action == "await_strategy_upload" else CONFIGS_DIR
        gh_put_file(PRIVATE_REPO, f"{target_dir}/{doc['file_name']}", content,
                    f"meta-bot: upload {doc['file_name']}")
        name = pending["bot"]
        if action == "await_strategy_upload":
            update_bot_field(name, [("STRATEGY_CLASS", doc["file_name"][:-3])])
        else:
            update_bot_field(name, [("BASE_CONFIG", doc["file_name"])])
        send(chat_id, f"✅ Uploaded and applied to {name} for its next restart.")
        del state["pending"][chat_id]

    elif step == "await_config_upload_newbot":
        gh_put_file(PRIVATE_REPO, f"{CONFIGS_DIR}/{doc['file_name']}", content,
                    f"meta-bot: upload {doc['file_name']}")
        pending["config"] = doc["file_name"]
        pending["step"] = "strategy"
        strategies = gh_list_dir(PRIVATE_REPO, STRATEGIES_DIR)
        buttons = [[{"text": s, "callback_data": f"newbot_strategy:{s}"}] for s in strategies if s.endswith(".py")]
        buttons.append([{"text": "⬆️ Upload new strategy file", "callback_data": "newbot_strategy_upload"}])
        send(chat_id, f"✅ Config uploaded. Pick a strategy:", inline=buttons)
        state["pending"][chat_id] = pending

    elif step == "await_strategy_upload_newbot":
        gh_put_file(PRIVATE_REPO, f"{STRATEGIES_DIR}/{doc['file_name']}", content,
                    f"meta-bot: upload {doc['file_name']}")
        pending["strategy"] = doc["file_name"][:-3]
        pending["step"] = "dry_run_choice"
        send(chat_id, "✅ Strategy uploaded.")
        prompt_dry_run_choice(chat_id)
        state["pending"][chat_id] = pending

# ---------------------------------------------------------------------------
# Watchdog mode — runs as a subprocess inside the same bot job
# ---------------------------------------------------------------------------

def run_watchdog_mode(bot_name, bot_dir):
    """Runs as a SECOND background process inside the SAME job as
    `freqtrade trade`, polling its local REST API on 127.0.0.1.

    Key fixes vs previous version:
    - poll_interval reduced to 2s for faster cap/alert reaction
    - equity = /balance.total + sum(profit_abs) from /status, matching
      watchdog8.py — gives true real-time mark-to-market not just settled cash
    - ratchet alerting: alert and cap each fire once per genuine NEW threshold
      crossing, re-arm only on true recovery-then-redrop or explicit HWM reset
    - alert branch gated by auto_closed so it never fires after a cap
    - reload_config flag: watchdog calls /reload_config directly (~2s latency)
      instead of triggering a full bot restart (~5min)
    - stopentry re-applied on startup if auto_closed was already True
      (covers the restart-without-recovery scenario)
    """
    import signal
    import subprocess

    config_path = f"{bot_dir}/watchdog_config.json"
    state_path = f"{bot_dir}/watchdog_state.json"
    secrets_path = f"{bot_dir}/secrets.json"

    # Local signal file written by the bot's shell loop immediately after
    # git pull. The watchdog reads this off local tmpfs — no GitHub round-trip,
    # effectively instant. Bot name is in the path so multiple bots on the same
    # runner never cross-signal (shouldn't happen, but safe by construction).
    reload_signal_path = f"/tmp/ft_reload_config_{bot_name}"

    # 2s poll — tight enough to be reactive on 1m candles, local API can
    # trivially handle this (it's 127.0.0.1, essentially zero overhead).
    poll_interval = 2
    api_base = "http://127.0.0.1:8080/api/v1"

    running = {"value": True}

    def handle_sigterm(signum, frame):
        running["value"] = False

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    def git(*args):
        r = subprocess.run(["git", *args], capture_output=True)
        return r.returncode == 0

    def commit_and_push(paths, message):
        git("add", *paths)
        git("commit", "-q", "-m", message)
        for attempt in range(5):
            if git("push", "-q"):
                return True
            git("pull", "--rebase", "-q")
            time.sleep(attempt * 2)
        return False

    def load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def api_auth():
        creds = load_json(secrets_path, {}).get("api_server", {})
        return (creds.get("username", "watchdog"), creds.get("password", "watchdog"))

    def fetch_equity():
        """Combines /balance (settled wallet) + sum of profit_abs from /status
        (unrealized P&L on open positions) — the same method watchdog8.py used.
        /balance.total alone can lag on open positions; this gives true real-time
        mark-to-market equity regardless of Freqtrade version or dry/live mode."""
        try:
            balance_r = requests.get(f"{api_base}/balance", auth=api_auth(), timeout=5)
            balance_r.raise_for_status()
            total_balance = float(balance_r.json().get("total", 0))

            status_r = requests.get(f"{api_base}/status", auth=api_auth(), timeout=5)
            status_r.raise_for_status()
            unrealized_pnl = sum(
                float(t.get("profit_abs", 0)) for t in status_r.json()
            )
            return total_balance + unrealized_pnl
        except Exception:
            return None

    def fetch_open_trade_count():
        try:
            r = requests.get(f"{api_base}/status", auth=api_auth(), timeout=5)
            r.raise_for_status()
            return len(r.json())
        except Exception:
            return None

    def send_telegram(text):
        tg_creds = load_json(secrets_path, {}).get("telegram", {})
        token, chat_id = tg_creds.get("token"), tg_creds.get("chat_id")
        if not token or not chat_id:
            return
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                           timeout=10)
        except Exception:
            pass

    def call_stopentry():
        try:
            requests.post(f"{api_base}/stopentry", auth=api_auth(), timeout=10)
        except Exception:
            pass

    def auto_close_positions():
        """Stops new entries and force-exits everything open."""
        call_stopentry()
        try:
            requests.post(f"{api_base}/forceexit", auth=api_auth(),
                           json={"tradeid": "all"}, timeout=15)
        except Exception:
            pass

    def call_reload_config():
        """Freqtrade's native in-place reload — no restart, no downtime."""
        try:
            requests.post(f"{api_base}/reload_config", auth=api_auth(), timeout=10)
            return True
        except Exception:
            return False

    state = load_json(state_path, {
        "hwm": 0.0,
        "auto_closed": False,
        # Ratchet state: tracks the last equity level where each threshold
        # FIRED — re-arms only when equity genuinely recovers above hwm
        # (drawdown) or drops back below alert level (then re-crosses it).
        # For cap: fires once, stays fired until explicit HWM reset.
        "dd_alert_armed": True,      # True = ready to fire, False = waiting for recovery
        "last_equity": None,
        "last_updated_ts": 0.0,
        "last_peak_alert_ts": {},
    })
    state.setdefault("dd_alert_armed", True)
    state.setdefault("last_peak_alert_ts", {})
    last_committed_hwm = state["hwm"]

    # Re-apply stopentry immediately on startup if we were already auto_closed
    # before this run. Covers bot restart without recovery — the cap fired for
    # a reason; a new Freqtrade process doesn't change that.
    if state.get("auto_closed"):
        # Give Freqtrade a moment to come up before hitting its API
        time.sleep(8)
        call_stopentry()

    # Throttle git pull to once per 60s. All time-critical signals (reload_config)
    # now arrive via the local signal file written by the shell loop — no need to
    # hammer GitHub every 2s. Non-urgent flags (reset_requested, alert thresholds)
    # are fine with a 60s delivery window. Reduces network noise and eliminates
    # git rebase conflicts with the bot's own 5-min commit cycle.
    _last_git_pull = 0.0
    GIT_PULL_INTERVAL = 60

    while running["value"]:
        time.sleep(poll_interval)

        now = time.time()
        if now - _last_git_pull >= GIT_PULL_INTERVAL:
            git("pull", "--rebase", "-q")
            _last_git_pull = now

        cfg = load_json(config_path, {"alerts_enabled": False})

        # --- Reload-config flag: respond within ~2s, no restart needed ---
        # Check the local signal file FIRST — it's written by the bot's shell
        # loop immediately after git pull, so it arrives here with no network
        # latency at all (local tmpfs read). The git-sourced flag in cfg is the
        # authoritative source; the signal file is just the fast local courier.
        # We clear both atomically so there's no double-fire.
        local_signal = os.path.exists(reload_signal_path)
        if local_signal or cfg.get("reload_config_requested"):
            if call_reload_config():
                # Clear local signal file immediately
                try:
                    os.remove(reload_signal_path)
                except FileNotFoundError:
                    pass
                # Clear the git-side flag and commit so meta_bot sees it done
                cfg["reload_config_requested"] = False
                save_json(config_path, cfg)
                commit_and_push([config_path],
                                f"watchdog: cleared reload_config flag - {bot_name}")
                send_telegram(f"⚙️ <b>Config reloaded</b> — {bot_name}\nTook effect in-place, no restart.")
                # Also forward to whatever's configured in broadcast_config.json
                # (separate from the owner-only message above — broadcast
                # destinations only get this if "Config reloaded" is enabled
                # for them, default ON, in their own field settings).
                try:
                    requests.post("http://127.0.0.1:9000/internal/broadcast",
                                   json={"type": "reload"}, timeout=5)
                except Exception:
                    pass  # relay may not be running yet/anymore — non-critical

        # Reset is honored even if monitoring is currently OFF.
        if cfg.get("reset_requested"):
            state["hwm"] = 0.0
            state["auto_closed"] = False
            state["dd_alert_armed"] = True
            state["last_peak_alert_ts"] = {}
            cfg["reset_requested"] = False
            save_json(config_path, cfg)
            commit_and_push([config_path], f"watchdog: cleared reset flag - {bot_name}")

        if not cfg.get("alerts_enabled", False):
            continue

        equity = fetch_equity()
        if equity is None:
            continue

        now = time.time()
        state["last_equity"] = equity
        state["last_updated_ts"] = now

        # --- HWM tracking ---
        if state["hwm"] == 0.0 or equity > state["hwm"]:
            state["hwm"] = equity
            # When equity makes a new high, re-arm the drawdown alert so it
            # can fire again if we then drop from this new peak.
            state["dd_alert_armed"] = True

        # --- Drawdown ratchet ---
        if state["hwm"] > 0 and not state["auto_closed"]:
            dd_pct = (state["hwm"] - equity) / state["hwm"] * 100
            dd_cap = cfg.get("drawdown_cap_pct")
            dd_alert = cfg.get("drawdown_alert_pct")

            # Cap: fires once per auto_closed=False period. Once fired,
            # stays silent until explicit HWM reset — regardless of whether
            # the bot restarts or equity drops further. No spam, no re-fire
            # on deeper loss without recovery.
            if dd_cap and dd_pct >= dd_cap:
                send_telegram(
                    f"🔴 <b>DRAWDOWN CAP BREACHED — AUTO-CLOSED</b> — {bot_name}\n"
                    f"Drop: {dd_pct:.2f}% (cap {dd_cap}%)\n"
                    f"HWM: ${state['hwm']:.2f} → now ${equity:.2f}\n"
                    f"Won't re-trigger until you Reset HWM from the meta-bot."
                )
                auto_close_positions()
                state["auto_closed"] = True
                state["dd_alert_armed"] = False  # cap fired — silence alert branch too

            # Alert: ratchet — fires when crossing the threshold downward,
            # re-arms only when equity recovers back above (hwm - alert_pct).
            # This means one message per genuine new drawdown event, not per poll.
            elif dd_alert and dd_pct >= dd_alert and state["dd_alert_armed"]:
                send_telegram(
                    f"⚠️ <b>DRAWDOWN ALERT</b> — {bot_name}\n"
                    f"Drop: {dd_pct:.2f}% (alert {dd_alert}%)\n"
                    f"HWM: ${state['hwm']:.2f} → now ${equity:.2f}"
                )
                state["dd_alert_armed"] = False  # disarm until equity recovers

            # Re-arm alert when equity recovers above the alert threshold
            elif dd_alert and dd_pct < dd_alert and not state["dd_alert_armed"]:
                state["dd_alert_armed"] = True

        # Once auto-closed, re-enable if new open trades appear (genuine fresh
        # start by the trader) — but do NOT re-enable just because equity moved.
        if state["auto_closed"]:
            open_count = fetch_open_trade_count()
            if open_count and open_count > 0:
                state["auto_closed"] = False
                state["dd_alert_armed"] = True

        # --- Peak ratchet: multiple alert levels, one cap ---
        peak_cap = cfg.get("peak_cap_pct")
        peak_levels = cfg.get("peak_alert_levels", [])

        if (peak_cap or peak_levels) and state["hwm"] > 0 and not state["auto_closed"]:
            # Use the HWM at the time of the last reset as the base for peak %.
            # _peak_base is set once on first entry and cleared on reset.
            if "_peak_base" not in state["last_peak_alert_ts"]:
                state["last_peak_alert_ts"]["_peak_base"] = equity
            base = state["last_peak_alert_ts"]["_peak_base"]
            gain_pct = (equity - base) / base * 100 if base and equity > base else 0

            if peak_cap and gain_pct >= peak_cap:
                send_telegram(
                    f"🟢 <b>PEAK CAP BREACHED — AUTO-CLOSED</b> — {bot_name}\n"
                    f"Gain: +{gain_pct:.2f}% (cap {peak_cap}%)\n"
                    f"Won't re-trigger until you Reset HWM from the meta-bot."
                )
                auto_close_positions()
                state["auto_closed"] = True

            for level in peak_levels:
                key = str(level)
                already_fired = state["last_peak_alert_ts"].get(key, 0) > 0
                if gain_pct >= level and not already_fired:
                    send_telegram(
                        f"🚀 <b>PEAK ALERT</b> — {bot_name}\n"
                        f"Gain: +{gain_pct:.2f}% (level {level}%)\nNow: ${equity:.2f}"
                    )
                    state["last_peak_alert_ts"][key] = now

        # Commit when HWM changed or every 60s as a heartbeat.
        if (state["hwm"] != last_committed_hwm
                or now - state.get("_last_commit_ts", 0) > 60):
            save_json(state_path, state)
            commit_and_push([state_path], f"autosave: watchdog state - {bot_name}")
            last_committed_hwm = state["hwm"]
            state["_last_commit_ts"] = now

    # Final commit on SIGTERM.
    save_json(state_path, state)
    if not commit_and_push([state_path], f"autosave: final watchdog state - {bot_name}"):
        print(f"WARNING: final watchdog state commit for {bot_name} failed after retries.")


# ---------------------------------------------------------------------------
# Relay — runs alongside Freqtrade and the watchdog for the bot's whole
# ~5h50m cycle. Freqtrade's own `webhook` config (set in secrets.json,
# format=json) posts every entry/exit/status event here on localhost;
# this fans each one out to whatever's configured in broadcast_config.json
# — any number of Telegram chats and/or raw webhook URLs, each with its
# own scale, field filters, active/paused state, and stagger setting.
#
# Runs as a tiny stdlib HTTP server rather than Flask — this process has
# the same lifetime and trust boundary as the watchdog (one ephemeral
# GitHub Actions runner, never reachable from outside it), so there's no
# reason to add a dependency for it.
# ---------------------------------------------------------------------------

def run_relay_mode(bot_name, bot_dir):
    import signal
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    config_path = f"{bot_dir}/broadcast_config.json"
    status_path = f"{bot_dir}/relay_status.json"
    RELAY_PORT = 9000

    running = {"value": True}

    def handle_sigterm(signum, frame):
        running["value"] = False

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    def git(*args):
        r = subprocess.run(["git", *args], capture_output=True)
        return r.returncode == 0

    def load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default

    # Visible, durable proof-of-life for this process — independent of
    # whether print() output actually makes it into the GitHub Actions log
    # for a long-lived backgrounded process (couldn't conclusively confirm
    # either way; this sidesteps the question entirely by using the same
    # git-commit channel every other piece of state in this system already
    # relies on). Written to disk immediately and unconditionally on every
    # event; the git commit itself is throttled (see _last_status_commit
    # below) rather than firing on every single trade — the watchdog and
    # the shell loop both also commit to this same working directory on
    # their own independent schedules, with no locking between any of the
    # three. Committing on every event measurably increased collisions
    # between all three (manifesting as "cannot pull with rebase: you have
    # unstaged changes" / failed pushes) rather than just being occasional
    # bad luck — so this status file trades a little staleness (at most
    # one throttle interval old) for not making that race meaningfully
    # worse than it already was.
    status = {"started_at": datetime.now().isoformat(), "events_received": 0,
              "last_event_type": None, "last_event_at": None, "last_error": None}
    try:
        with open(status_path, "w") as f:
            json.dump(status, f, indent=2)
        subprocess.run(["git", "add", status_path], capture_output=True)
        subprocess.run(["git", "commit", "-m", f"relay: started - {bot_name}", "-q"], capture_output=True)
        subprocess.run(["git", "push", "-q"], capture_output=True)
    except Exception as e:
        print(f"[relay] {bot_name}: FAILED to write initial status file: {e}")

    _last_status_commit = {"ts": 0.0}
    STATUS_COMMIT_INTERVAL = 60  # matches GIT_PULL_INTERVAL below — one shared rhythm

    def record_event(event_type, error=None):
        status["events_received"] += 1
        status["last_event_type"] = event_type
        status["last_event_at"] = datetime.now().isoformat()
        if error:
            status["last_error"] = str(error)[:300]
        try:
            with open(status_path, "w") as f:
                json.dump(status, f, indent=2)
        except Exception:
            return  # disk write failed — nothing to commit

        now = time.time()
        if now - _last_status_commit["ts"] < STATUS_COMMIT_INTERVAL:
            return  # disk is current; git catches up on the next throttled pass
        _last_status_commit["ts"] = now
        try:
            git("add", status_path)
            git("commit", "-m", f"relay: status update - {bot_name}", "-q")
            if not git("push", "-q"):
                git("pull", "--rebase", "-q")
                git("push", "-q")
        except Exception:
            pass  # best-effort — never let status bookkeeping break a real event

    # Config is read fresh off disk on every event (not cached) — events
    # are rare relative to the 60s git-pull cadence below, so the disk read
    # itself is essentially free, and this guarantees a config change made
    # from the meta-bot a moment ago is honored on the very next trade
    # rather than possibly one stale poll cycle behind.
    _last_git_pull = {"ts": 0.0}
    GIT_PULL_INTERVAL = 60

    def maybe_pull():
        now = time.time()
        if now - _last_git_pull["ts"] >= GIT_PULL_INTERVAL:
            git("pull", "--rebase", "-q")
            _last_git_pull["ts"] = now

    def fan_out(event_type, data):
        cfg = load_json(config_path, {"enabled": False, "stagger_seconds": 1.0, "destinations": {}})
        if not cfg.get("enabled"):
            return
        stagger = max(0.0, float(cfg.get("stagger_seconds", 1.0)))
        first_telegram_sent = False

        for dest in cfg.get("destinations", {}).values():
            if not dest.get("active", True):
                continue
            fields, scale = dest.get("fields", {}), dest.get("scale", 1.0)

            if dest["type"] == "telegram":
                text = render_broadcast_message(event_type, data, fields, scale)
                if not text:
                    continue  # this event type is switched off for this destination
                if dest.get("stagger") and first_telegram_sent and stagger > 0:
                    time.sleep(stagger)
                ok, detail = send_to_destination(dest, text)
                first_telegram_sent = True
            else:  # webhook — structured fields, not rendered text
                payload = build_webhook_payload(bot_name, event_type, data, fields, scale)
                if payload is None:
                    continue  # this event type is switched off for this destination
                ok, detail = send_to_destination(dest, None, payload=payload)

            if not ok:
                print(f"[relay] {bot_name}: failed to reach {dest.get('label', '?')}: {detail}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # don't spam the job log with one line per request

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode() or "{}")
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            if self.path == "/webhook":
                maybe_pull()
                # Freqtrade's own webhook config (format=json) is the source
                # of "type" — we set it explicitly per event in secrets.json
                # generation so it always matches what render_broadcast_message
                # expects, regardless of what Freqtrade calls the event
                # internally.
                event_type = data.get("type", "unknown")
                record_event(event_type)
                fan_out(event_type, data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            elif self.path == "/internal/broadcast":
                # Loopback-only, fired by the shell loop (process-died case,
                # which Freqtrade's own webhookstatus never sees since it
                # never got to shut down cleanly) and by the watchdog
                # (reload-config confirmation) — both co-located on this
                # same runner. Not Freqtrade's webhook shape, so it's a
                # separate path rather than overloading /webhook.
                maybe_pull()
                event_type = data.get("type", "status")
                record_event(event_type)
                fan_out(event_type, data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"running"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"running"}')
            else:
                self.send_response(404)
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", RELAY_PORT), Handler)
    server.timeout = 1.0  # so handle_request() below returns promptly for the SIGTERM check

    print(f"[relay] {bot_name}: listening on 127.0.0.1:{RELAY_PORT}")
    while running["value"]:
        server.handle_request()
    server.server_close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print(f"Meta-bot starting. OWNER_CHAT_ID={OWNER_CHAT_ID!r}")

    state = load_state()
    end_time = time.time() + STOP_AFTER_SECONDS

    while time.time() < end_time:
        updates = tg("getUpdates", offset=state["offset"], timeout=30)
        offset_before = state["offset"]
        for upd in updates:
            state["offset"] = upd["update_id"] + 1

            if "callback_query" in upd:
                cq = upd["callback_query"]
                if str(cq["message"]["chat"]["id"]) != OWNER_CHAT_ID:
                    continue
                if cq["data"].startswith("newbot_") or cq["data"].startswith("postcreate_"):
                    handle_new_bot_callback(state, cq)
                else:
                    handle_callback(state, cq)

            elif "message" in upd:
                msg = upd["message"]
                chat_id = str(msg["chat"]["id"])
                if chat_id != OWNER_CHAT_ID:
                    print(f"Dropped message from chat_id={chat_id!r} — doesn't match OWNER_CHAT_ID={OWNER_CHAT_ID!r}")
                    continue

                if "document" in msg:
                    handle_document(state, chat_id, msg)
                    continue

                text = msg.get("text", "")
                pending_action = state["pending"].get(chat_id, {}).get("action")

                if text == "🔙 Back":
                    state["pending"].pop(chat_id, None)
                    send(chat_id, "Back to the main menu.")
                elif text == "📋 List Bots":
                    state["pending"].pop(chat_id, None)
                    show_bot_list(chat_id)
                elif text == "📊 Status":
                    state["pending"].pop(chat_id, None)
                    show_status(chat_id)
                elif text == "ℹ️ Help" or text == "/start":
                    send(chat_id, "Live bot library control panel. Use the buttons below.")
                elif text == "➕ New Bot":
                    state["pending"].pop(chat_id, None)
                    handle_new_bot_flow(state, chat_id, msg)
                elif text == "🛠 Custom Bot":
                    state["pending"].pop(chat_id, None)
                    handle_custom_bot_flow(state, chat_id, msg)
                elif text.startswith("/confirm_delete_custom_"):
                    name = text[len("/confirm_delete_custom_"):]
                    delete_custom_bot_entirely(name)
                    send(chat_id, f"💀 Custom bot {name} deleted entirely.")
                elif text.startswith("/confirm_delete_entirely_"):
                    name = text[len("/confirm_delete_entirely_"):]
                    delete_bot_entirely(name)
                    send(chat_id, f"💀 {name} deleted entirely — workflow, DB, secrets, all of it.")
                elif text.startswith("/confirm_delete_"):
                    name = text[len("/confirm_delete_"):]
                    n_artifacts = delete_bot_db(name)
                    artifact_note = f", {n_artifacts} stale artifact(s) cleared" if n_artifacts else ""
                    send(chat_id, f"✅ DB deleted for {name} despite open trades (live file + "
                                   f"git snapshot{artifact_note}) — exchange stop is your only "
                                   f"protection now until it restarts and re-syncs.")

                elif pending_action == "await_times":
                    handle_times_input(state, chat_id, text)
                elif pending_action == "await_wd_value":
                    handle_wd_value_input(state, chat_id, text)
                elif pending_action == "await_wd_peak_levels":
                    handle_wd_peak_levels_input(state, chat_id, text)
                elif pending_action == "await_hp_field":
                    handle_hp_field_input(state, chat_id, text)
                elif pending_action == "await_strategy_text":
                    handle_strategy_text_input(state, chat_id, text)
                elif pending_action == "await_config_text":
                    handle_config_text_input(state, chat_id, text)
                elif pending_action == "await_bc_stagger":
                    handle_bc_stagger_input(state, chat_id, text)
                elif pending_action == "await_bc_new_dest":
                    handle_bc_new_dest_input(state, chat_id, text)
                elif pending_action == "await_bc_rename":
                    handle_bc_rename_input(state, chat_id, text)
                elif pending_action == "await_bc_scale":
                    handle_bc_scale_input(state, chat_id, text)
                elif pending_action == "await_bc_editurl":
                    handle_bc_editurl_input(state, chat_id, text)
                elif pending_action == "await_bc_editchatid":
                    handle_bc_editchatid_input(state, chat_id, text)
                elif pending_action == "await_bc_test_text":
                    handle_bc_test_text_input(state, chat_id, text)
                elif chat_id in state["pending"] and "step" in state["pending"].get(chat_id, {}):
                    handle_new_bot_flow(state, chat_id, msg)
                elif chat_id in state["pending"] and state["pending"][chat_id].get("custom_bot_step"):
                    handle_custom_bot_flow(state, chat_id, msg)
                else:
                    send(chat_id, "Not sure what that means — use the buttons below.")

        check_schedules(state)

        if state["offset"] != offset_before:
            save_state(state)

    save_state(state)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "watchdog":
        run_watchdog_mode(bot_name=sys.argv[2], bot_dir=sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "relay":
        run_relay_mode(bot_name=sys.argv[2], bot_dir=sys.argv[3])
    else:
        missing = [n for n, v in [("META_BOT_TOKEN", META_BOT_TOKEN),
                                   ("META_BOT_GITHUB_PAT", GITHUB_PAT),
                                   ("OWNER_CHAT_ID", OWNER_CHAT_ID),
                                   ("GITHUB_REPOSITORY_OWNER", REPO_OWNER)] if not v]
        if missing:
            raise SystemExit(f"Missing required env vars for serve mode: {missing}")
        main()
