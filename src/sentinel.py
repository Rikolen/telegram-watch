"""
Telegram Sentinel — btc-panel integration microservice.

Monitors a Telegram channel via MTProto (Telethon), detects content sections
(Liquidez → screenshots, Operativa → videos, general → HTML synthesis),
and pushes content to the btc-panel bridge.

Session bootstrap (one-time, interactive):
  docker run --rm -it -v /opt/appdata/telegram-watch/data:/data \
    --env-file .env telegram-sentinel:local python auth.py
"""
import asyncio
import base64
import json
import logging
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

# ── Config ─────────────────────────────────────────────────────────────────
API_ID          = int(os.environ["TELEGRAM_API_ID"])
API_HASH        = os.environ["TELEGRAM_API_HASH"]
SESSION_FILE    = os.environ.get("TELEGRAM_SESSION_FILE", "/data/telegram.session")
CHANNELS        = [c.strip() for c in os.environ["TELEGRAM_CHANNELS"].split(",")]
# Channels that require an explicit section keyword (liquidez/operativa) before
# saving media. Channels NOT in this set save all media freely.
# Default: empty (all channels permissive). Set to War Room ID to enforce strict mode.
_strict_raw        = os.environ.get("STRICT_MEDIA_CHANNELS", "")
STRICT_MEDIA_CHANNELS = {c.strip() for c in _strict_raw.split(",") if c.strip()}

BRIDGE_URL      = os.environ.get("BRIDGE_URL", "http://btc-panel-backend:3003")
BRIDGE_ENABLED  = os.environ.get("BRIDGE_ENABLED", "true").lower() == "true"
BRIDGE_TIMEOUT  = int(os.environ.get("BRIDGE_TIMEOUT", "60"))

NTFY_URL        = os.environ.get("NTFY_URL", "")
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "crypto-sentinel")
NTFY_TOKEN      = os.environ.get("NTFY_TOKEN", "")
NTFY_ENABLED    = os.environ.get("NTFY_ENABLED", "false").lower() == "true" and bool(NTFY_URL)

VIKUNJA_URL     = os.environ.get("VIKUNJA_URL", "")
VIKUNJA_TOKEN   = os.environ.get("VIKUNJA_TOKEN", "")
VIKUNJA_PROJECT = int(os.environ.get("VIKUNJA_PROJECT_ID", "0"))
VIKUNJA_ENABLED = os.environ.get("VIKUNJA_ENABLED", "false").lower() == "true" and bool(VIKUNJA_URL)

N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
N8N_ENABLED     = os.environ.get("N8N_ENABLED", "false").lower() == "true" and bool(N8N_WEBHOOK_URL)

# AI filter — applied at synthesis time to War Room/Cuartel texts (no section detected).
# Uses Claude to discard noise (chatter, memes, off-topic) and keep trading signals.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_FILTER_ENABLED = os.environ.get("AI_FILTER_ENABLED", "false").lower() == "true" and bool(ANTHROPIC_API_KEY)
AI_FILTER_MODEL   = os.environ.get("AI_FILTER_MODEL", "claude-haiku-4-5-20251001")

# Section keyword patterns (case-insensitive, comma-separated)
LIQUIDEZ_KW   = [k.strip().lower() for k in os.environ.get(
    "LIQUIDEZ_PATTERNS", "liquidez,liquidity,liq"
).split(",") if k.strip()]
OPERATIVA_KW  = [k.strip().lower() for k in os.environ.get(
    "OPERATIVA_PATTERNS", "operativa,operation,op"
).split(",") if k.strip()]

# Context window: scan last N messages for section header before deciding section
CONTEXT_WINDOW  = int(os.environ.get("CONTEXT_WINDOW", "5"))

# HTML synthesis window in hours — only include messages from the last N hours
SYNTHESIS_HOURS = int(os.environ.get("SYNTHESIS_HOURS", "24"))

MEDIA_DIR  = Path(os.environ.get("MEDIA_DIR", "/data/media"))
LOG_JSONL  = Path(os.environ.get("LOG_JSONL", "/data/sentinel.jsonl"))
WEB_PORT   = int(os.environ.get("WEB_PORT", "8765"))
WEB_HOST   = os.environ.get("WEB_HOST", "0.0.0.0")

# ── State ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sentinel")

_state: dict = {
    "status": "starting",
    "connected": False,
    "channels": CHANNELS,
    "channel_names": {},   # id_str → resolved display name ("🪖 War Room")
    "recent_events": deque(maxlen=500),
    "stats": {"photos": 0, "videos": 0, "reports": 0, "errors": 0, "n8n_triggers": 0},
    # per-channel context window: channel → deque of recent (msg_id, text, section)
    "context": {},
}


# ── Section detection ───────────────────────────────────────────────────────

def _detect_section(text: str) -> Optional[str]:
    """Return 'liquidez', 'operativa', or None from message text."""
    t = text.lower()
    for kw in OPERATIVA_KW:
        if kw in t:
            return "operativa"
    for kw in LIQUIDEZ_KW:
        if kw in t:
            return "liquidez"
    return None


def _infer_section_from_context(channel: str, current_text: str) -> Optional[str]:
    """
    Look at recent context messages for this channel to infer section.
    A text message with a section keyword that precedes media implies the media
    belongs to that section.
    """
    # First check the current message itself
    section = _detect_section(current_text or "")
    if section:
        return section
    # Walk context backward
    ctx = _state["context"].get(channel, deque())
    for _, msg_text, msg_section in reversed(list(ctx)):
        if msg_section:
            return msg_section
        if msg_text and _detect_section(msg_text):
            return _detect_section(msg_text)
    return None


def _push_context(channel: str, msg_id: int, text: str, section: Optional[str]) -> None:
    if channel not in _state["context"]:
        _state["context"][channel] = deque(maxlen=CONTEXT_WINDOW)
    _state["context"][channel].appendleft((msg_id, text, section))


# ── Bridge helpers ──────────────────────────────────────────────────────────

async def _bridge_save_base64(path: str, data: bytes, mime: str) -> bool:
    """POST base64-encoded content to /api/narrativa/save."""
    if not BRIDGE_ENABLED:
        return True
    b64 = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    payload = {"path": path, "content": b64, "encoding": "base64"}
    try:
        async with httpx.AsyncClient(timeout=BRIDGE_TIMEOUT) as client:
            r = await client.post(f"{BRIDGE_URL}/api/narrativa/save", json=payload)
            r.raise_for_status()
            log.info("bridge save ok: %s", path)
            return True
    except Exception as exc:
        log.error("bridge save error for %s: %s", path, exc)
        _state["stats"]["errors"] += 1
        return False


async def _bridge_upload_binary(path: str, data: bytes, content_type: str) -> bool:
    """POST raw binary to /api/narrativa/upload?path=... (better for large videos)."""
    if not BRIDGE_ENABLED:
        return True
    try:
        async with httpx.AsyncClient(timeout=BRIDGE_TIMEOUT) as client:
            r = await client.post(
                f"{BRIDGE_URL}/api/narrativa/upload",
                params={"path": path},
                content=data,
                headers={"Content-Type": content_type},
            )
            r.raise_for_status()
            log.info("bridge upload ok: %s (%d bytes)", path, len(data))
            return True
    except Exception as exc:
        log.error("bridge upload error for %s: %s", path, exc)
        _state["stats"]["errors"] += 1
        return False


async def _bridge_save_html(path: str, html: str) -> bool:
    """POST HTML text to /api/narrativa/save."""
    if not BRIDGE_ENABLED:
        return True
    payload = {"path": path, "content": html}
    try:
        async with httpx.AsyncClient(timeout=BRIDGE_TIMEOUT) as client:
            r = await client.post(f"{BRIDGE_URL}/api/narrativa/save", json=payload)
            r.raise_for_status()
            log.info("bridge html ok: %s", path)
            return True
    except Exception as exc:
        log.error("bridge html error for %s: %s", path, exc)
        _state["stats"]["errors"] += 1
        return False


async def _save_companion_json(path: str, meta: dict) -> bool:
    """Save a .json companion file alongside the media."""
    payload = {"path": path, "content": json.dumps(meta, ensure_ascii=False, indent=2)}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{BRIDGE_URL}/api/narrativa/save", json=payload)
            r.raise_for_status()
            return True
    except Exception as exc:
        log.error("companion json error for %s: %s", path, exc)
        return False


# ── Notification helpers ────────────────────────────────────────────────────

async def _ntfy_push(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    if not NTFY_ENABLED:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{NTFY_URL}/{NTFY_TOPIC}", content=message.encode(), headers=headers)
    except Exception as exc:
        log.warning("ntfy error: %s", exc)


async def _vikunja_create_task(title: str, description: str) -> None:
    if not VIKUNJA_ENABLED or not VIKUNJA_PROJECT:
        return
    payload = {"title": title, "description": description, "project_id": VIKUNJA_PROJECT}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{VIKUNJA_URL}/api/v1/tasks",
                json=payload,
                headers={"Authorization": f"Bearer {VIKUNJA_TOKEN}"},
            )
            r.raise_for_status()
            log.info("vikunja task created: %s", title)
    except Exception as exc:
        log.warning("vikunja error: %s", exc)


async def _n8n_trigger(event_type: str, payload: dict) -> None:
    if not N8N_ENABLED:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(N8N_WEBHOOK_URL, json={"type": event_type, **payload})
            _state["stats"]["n8n_triggers"] += 1
    except Exception as exc:
        log.warning("n8n webhook error: %s", exc)


# ── Telethon event handler ──────────────────────────────────────────────────

async def handle_message(event, channel_id: str) -> None:
    msg = event.message
    text = msg.text or msg.message or ""
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # Determine section from message text and context
    direct_section = _detect_section(text)
    section = direct_section or _infer_section_from_context(channel_id, text)

    # Update context with this message
    _push_context(channel_id, msg.id, text, direct_section)

    # ── Photo → Liquidez screenshots ─────────────────────────────────────
    if msg.photo or (isinstance(getattr(msg, "media", None), MessageMediaPhoto)):
        log.info("[%s] photo msg=%d section=%s", channel_id, msg.id, section)
        if channel_id in STRICT_MEDIA_CHANNELS and section != "liquidez":
            log.debug("[%s] photo skipped — strict channel, section=%s (need liquidez)", channel_id, section)
            return
        actual_section = section or "liquidez"
        data = await msg.download_media(bytes)
        if not data:
            log.warning("photo download failed msg=%d", msg.id)
            return
        filename = f"liq-{ts}-{msg.id}.jpg"
        bridge_path = f"/screenshots/{filename}"
        ok = await _bridge_save_base64(bridge_path, data, "image/jpeg")
        if ok:
            _state["stats"]["photos"] += 1
            note_text = text[:300] if text else f"Liquidez screenshot from channel {channel_id}"
            companion = {
                "type": "screenshot",
                "section": actual_section,
                "channel": channel_id,
                "msg_id": msg.id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": note_text,
                "file": filename,
            }
            await _save_companion_json(f"/screenshots/{filename}.json", companion)
            _log_event("photo", filename, actual_section, note_text)
            await _n8n_trigger("screenshot", {"channel": channel_id, "section": actual_section,
                                               "file": filename, "note": note_text})

    # ── Video/Document → Operativa ──────────────────────────────────────
    elif isinstance(getattr(msg, "media", None), MessageMediaDocument):
        doc = msg.media.document
        if not doc:
            return
        mime = getattr(doc, "mime_type", "") or ""
        is_video = mime.startswith("video/") or any(
            getattr(a, "file_name", "").lower().endswith((".mp4", ".webm", ".mov"))
            for a in getattr(doc, "attributes", [])
        )
        if not is_video:
            return

        log.info("[%s] video msg=%d section=%s", channel_id, msg.id, section)
        if channel_id in STRICT_MEDIA_CHANNELS and section != "operativa":
            log.debug("[%s] video skipped — strict channel, section=%s (need operativa)", channel_id, section)
            return
        # Determine filename
        fname = None
        for attr in getattr(doc, "attributes", []):
            if hasattr(attr, "file_name") and attr.file_name:
                fname = attr.file_name
                break
        if not fname:
            fname = f"op-{ts}-{msg.id}.mp4"

        data = await msg.download_media(bytes)
        if not data:
            log.warning("video download failed msg=%d", msg.id)
            return

        bridge_path = f"/videos/{fname}"
        ok = await _bridge_upload_binary(bridge_path, data, mime or "video/mp4")
        if ok:
            _state["stats"]["videos"] += 1
            note_text = text[:300] if text else f"Operativa video from channel {channel_id}"
            companion = {
                "type": "video",
                "section": section or "operativa",
                "channel": channel_id,
                "msg_id": msg.id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": note_text,
                "file": fname,
                "size_bytes": len(data),
            }
            await _save_companion_json(f"/videos/{fname}.json", companion)
            _log_event("video", fname, section or "operativa", note_text)

            # ntfy + vikunja on new Operativa video
            await _ntfy_push(
                title="📹 Nuevo video Operativa",
                message=f"{note_text}\n{fname}",
                priority="high",
                tags="trading,video",
            )
            await _vikunja_create_task(
                title=f"📹 Revisar: {fname}",
                description=f"Canal: {channel_id}\n{note_text}\nArchivo: {bridge_path}",
            )
            await _n8n_trigger("video", {"channel": channel_id, "section": section or "operativa",
                                         "file": fname, "note": note_text})

    # ── Text message → general crypto news synthesis ──────────────────
    elif text and len(text) > 50:
        log.debug("[%s] text msg=%d len=%d", channel_id, msg.id, len(text))
        # Accumulate text messages; synthesis is triggered by schedule or manually via API
        _state["recent_events"].appendleft({
            "type": "text",
            "channel": channel_id,
            "msg_id": msg.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "section": section,
            "text": text[:1000],
        })
        if section:
            _log_event("text", f"msg-{msg.id}", section, text[:200])
            await _n8n_trigger("text", {"channel": channel_id, "section": section,
                                        "msg_id": msg.id, "text": text[:500]})


def _log_event(event_type: str, filename: str, section: str, note: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "file": filename,
        "section": section,
        "note": note[:200],
    }
    _state["recent_events"].appendleft({**entry})
    try:
        LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with LOG_JSONL.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        log.warning("log write error: %s", exc)


# ── AI filter ──────────────────────────────────────────────────────────────

_AI_FILTER_PROMPT = """\
You are a crypto trading signal filter. You will receive a numbered list of messages from a traders' chat.

Your task: identify which messages contain RELEVANT trading information.

KEEP (return their index):
- Price analysis, support/resistance levels, liquidation zones
- Trade setups, entries, targets, stop-loss levels
- Market news affecting crypto prices
- Technical analysis, chart patterns
- Macro events relevant to crypto

DISCARD (do NOT return their index):
- Personal conversation, greetings, jokes
- Memes, GIFs, off-topic content
- Single emojis or very short reactions
- Spam or repeated content
- Content unrelated to crypto/trading

Respond ONLY with a JSON array of the indices to KEEP. Example: [0, 2, 5]
If none are relevant, respond: []
"""

async def _ai_filter_cuartel(candidates: list[dict]) -> list[dict]:
    """Send War Room/Cuartel texts to Claude and return only trading-relevant ones."""
    if not candidates:
        return []
    if not AI_FILTER_ENABLED:
        return candidates

    import anthropic

    numbered = "\n".join(
        f"[{i}] {e['text'][:400]}" for i, e in enumerate(candidates)
    )
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=AI_FILTER_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": f"{_AI_FILTER_PROMPT}\n\nMessages:\n{numbered}"}],
        )
        raw = msg.content[0].text.strip()
        indices = json.loads(raw)
        if not isinstance(indices, list):
            raise ValueError("unexpected response format")
        kept = [candidates[i] for i in indices if isinstance(i, int) and 0 <= i < len(candidates)]
        log.info("AI filter: %d/%d Cuartel messages kept", len(kept), len(candidates))
        return kept
    except Exception as exc:
        log.warning("AI filter error (%s) — including all Cuartel messages as fallback", exc)
        return candidates


# ── HTML synthesis ──────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Sentinel — {date}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem;
         background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: .5rem; }}
  .entry {{ border-left: 3px solid #58a6ff; padding: .5rem 1rem; margin: .75rem 0;
            background: #161b22; border-radius: 0 4px 4px 0; }}
  .entry.liquidez {{ border-color: #3fb950; }}
  .entry.operativa {{ border-color: #f78166; }}
  .meta {{ font-size: .75rem; color: #8b949e; margin-bottom: .25rem; }}
  .text {{ white-space: pre-wrap; font-size: .9rem; }}
</style>
</head>
<body>
<h1>Crypto Sentinel — {date}</h1>
<p style="color:#8b949e">Generado: {generated_at} · {total} mensajes procesados</p>
{entries}
</body>
</html>
"""

_ENTRY_TEMPLATE = """\
<div class="entry {section_class}">
  <div class="meta">{timestamp} · {channel} · <strong>{section_label}</strong></div>
  <div class="text">{text}</div>
</div>"""


async def generate_html_report(client: TelegramClient) -> Optional[str]:
    """Generate HTML synthesis from text events within the last SYNTHESIS_HOURS window."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SYNTHESIS_HOURS)

    def _in_window(e: dict) -> bool:
        ts = e.get("timestamp", "")
        if not ts:
            return False
        try:
            return datetime.fromisoformat(ts) >= cutoff
        except ValueError:
            return False

    all_texts = [
        e for e in _state["recent_events"]
        if e.get("type") == "text" and _in_window(e)
    ]
    if not all_texts:
        return None

    # Separate: always-include vs Cuartel candidates needing AI filter.
    # Cuartel = strict channel (War Room) + no section detected.
    always_include = [
        e for e in all_texts
        if not (e.get("channel") in STRICT_MEDIA_CHANNELS and not e.get("section"))
    ]
    cuartel = [
        e for e in all_texts
        if e.get("channel") in STRICT_MEDIA_CHANNELS and not e.get("section")
    ]
    filtered_cuartel = await _ai_filter_cuartel(cuartel)

    events_list = sorted(
        always_include + filtered_cuartel,
        key=lambda e: e.get("timestamp", ""),
    )

    entries_html = []
    for e in events_list[-50:]:
        section = e.get("section") or "general"
        entries_html.append(_ENTRY_TEMPLATE.format(
            section_class=section,
            timestamp=e.get("timestamp", "")[:19].replace("T", " "),
            channel=e.get("channel", ""),
            section_label=section.upper(),
            text=e.get("text", "").replace("<", "&lt;").replace(">", "&gt;"),
        ))

    now = datetime.now(timezone.utc)
    html = _HTML_TEMPLATE.format(
        date=now.strftime("%Y-%m-%d"),
        generated_at=now.isoformat()[:19],
        total=len(events_list),
        entries="\n".join(entries_html),
    )
    slug = now.strftime("%Y-%m-%d-%H%M")
    path = f"/reports/crypto-sentinel-{slug}.html"
    ok = await _bridge_save_html(path, html)
    if ok:
        _state["stats"]["reports"] += 1
        return path
    return None


# ── FastAPI web UI ──────────────────────────────────────────────────────────

app = FastAPI(title="Telegram Sentinel", docs_url=None, redoc_url=None)
_telethon_client: Optional[TelegramClient] = None

_STATUS_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Telegram Sentinel</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem;
         background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; }}
  .badge {{ display:inline-block; padding:.2rem .6rem; border-radius:1rem; font-size:.8rem; font-weight:600; }}
  .ok {{ background:#1a4731; color:#3fb950; }}
  .err {{ background:#4d1f1f; color:#f85149; }}
  .warn {{ background:#3d2f04; color:#d29922; }}
  table {{ width:100%; border-collapse:collapse; margin-top:1rem; font-size:.85rem; }}
  th {{ background:#161b22; padding:.5rem; text-align:left; color:#8b949e; }}
  td {{ padding:.4rem .5rem; border-bottom:1px solid #21262d; }}
  .section-liquidez {{ color:#3fb950; }}
  .section-operativa {{ color:#f78166; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:1rem; margin:.75rem 0; }}
  .stat {{ display:inline-block; margin-right:2rem; }}
  .stat .num {{ font-size:1.5rem; font-weight:700; color:#58a6ff; }}
  .stat .lbl {{ font-size:.75rem; color:#8b949e; }}
</style>
</head>
<body>
<h1>📡 Telegram Sentinel</h1>
<div class="card">
  <span class="badge {status_class}">{status_label}</span>
  &nbsp; Canales: <code>{channels}</code>
</div>
<div class="card">
  <span class="stat"><div class="num">{stat_photos}</div><div class="lbl">Screenshots</div></span>
  <span class="stat"><div class="num">{stat_videos}</div><div class="lbl">Videos</div></span>
  <span class="stat"><div class="num">{stat_reports}</div><div class="lbl">Reports HTML</div></span>
  <span class="stat"><div class="num">{stat_errors}</div><div class="lbl">Errors</div></span>
  <span class="stat"><div class="num">{stat_n8n}</div><div class="lbl">n8n triggers</div></span>
</div>
<div class="card">
  <div style="margin-bottom:.5rem;font-weight:600">Últimos eventos</div>
  <table>
    <tr><th>Hora</th><th>Tipo</th><th>Sección</th><th>Archivo / Nota</th></tr>
    {rows}
  </table>
</div>
<p style="color:#8b949e;font-size:.75rem">
  Auto-recarga cada 30s · Síntesis: últimas <strong style="color:#c9d1d9">{synthesis_hours}h</strong> ·
  <button onclick="fetch('/api/report',{{method:'POST'}}).then(r=>r.json()).then(d=>alert(d.ok?'Guardado: '+d.path:d.reason||'sin eventos'))"
          style="background:none;border:none;color:#58a6ff;cursor:pointer;padding:0;font-size:.75rem">
    Generar síntesis HTML ahora
  </button>
  &nbsp;·&nbsp;
  <a href="/api/dialogs" target="_blank" style="color:#58a6ff;font-size:.75rem">Ver canales disponibles →</a>
</p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    st  = _state["stats"]
    ok  = _state["connected"]
    rows = []
    for e in list(_state["recent_events"])[:30]:
        section = e.get("section") or "—"
        sc = f"section-{section}" if section in ("liquidez", "operativa") else ""
        rows.append(
            f"<tr><td>{e.get('timestamp','')[:19].replace('T',' ')}</td>"
            f"<td>{e.get('type','')}</td>"
            f"<td class='{sc}'>{section}</td>"
            f"<td>{(e.get('file') or e.get('note',''))[:80]}</td></tr>"
        )
    status = _state["status"]
    if status == "auth_required":
        sc, sl = "err", "⚠ Auth requerida — ejecutar auth.py"
    elif ok:
        sc, sl = "ok", "● Conectado"
    else:
        sc, sl = "warn", "● Conectando…"

    names = _state["channel_names"]
    channel_display = ", ".join(
        names.get(ch, ch) for ch in _state["channels"]
    )

    return _STATUS_HTML.format(
        status_class=sc,
        status_label=sl,
        channels=channel_display,
        stat_photos=st["photos"],
        stat_videos=st["videos"],
        stat_reports=st["reports"],
        stat_errors=st["errors"],
        stat_n8n=st["n8n_triggers"],
        rows="\n".join(rows) if rows else "<tr><td colspan='4' style='color:#8b949e'>Sin eventos aún</td></tr>",
        synthesis_hours=SYNTHESIS_HOURS,
    )


@app.get("/health")
async def health():
    return {"ok": True, "connected": _state["connected"], "status": _state["status"]}


@app.get("/api/status")
async def api_status():
    names = _state["channel_names"]
    return JSONResponse({
        "status": _state["status"],
        "connected": _state["connected"],
        "channels": [
            {"id": ch, "name": names.get(ch, ch)} for ch in _state["channels"]
        ],
        "stats": _state["stats"],
    })


@app.get("/api/dialogs")
async def api_dialogs():
    """List all channels/groups the account belongs to, with their numeric IDs."""
    if _telethon_client is None or not _state["connected"]:
        raise HTTPException(503, "Sentinel not connected")
    dialogs = []
    async for d in _telethon_client.iter_dialogs():
        entity = d.entity
        dialogs.append({
            "id": d.id,
            "name": d.name,
            "type": type(entity).__name__,
            "unread": d.unread_count,
        })
    return JSONResponse({"count": len(dialogs), "dialogs": dialogs})


@app.post("/api/report")
async def api_report():
    """Manually trigger HTML synthesis report from accumulated text events."""
    if _telethon_client is None:
        raise HTTPException(503, "Sentinel not running")
    path = await generate_html_report(_telethon_client)
    if path:
        return {"ok": True, "path": path}
    return {"ok": False, "reason": "no text events accumulated yet"}


# ── Main loop ───────────────────────────────────────────────────────────────

async def run_sentinel():
    global _telethon_client
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    _telethon_client = client

    log.info("Connecting to Telegram · session: %s · channels: %s", SESSION_FILE, CHANNELS)
    _state["status"] = "connecting"

    # Use connect() NOT start() — start() is interactive and requires TTY.
    # If the session is missing or expired the container would crash with EOFError.
    await client.connect()

    if not await client.is_user_authorized():
        _state["status"] = "auth_required"
        log.error(
            "NOT AUTHORIZED — session missing or expired at '%s'.\n"
            "Bootstrap interactively on the host:\n"
            "  docker run --rm -it \\\n"
            "    -v ~/homelab/apps/telegram-watch/data:/data \\\n"
            "    -e TELEGRAM_API_ID=%s \\\n"
            "    -e TELEGRAM_API_HASH=<hash> \\\n"
            "    -e TELEGRAM_SESSION_FILE=/data/telegram.session \\\n"
            "    telegram-sentinel:local python auth.py\n"
            "Then restart the stack.",
            SESSION_FILE, API_ID,
        )
        # Keep alive so the web UI shows auth_required — poll every 30s in case
        # the user restarts the stack after fixing the session
        while True:
            await asyncio.sleep(30)
            await client.connect()
            if await client.is_user_authorized():
                log.info("Session now valid — continuing startup")
                break
        # If we reach here, loop again through normal startup path is complex;
        # just exit so Docker restarts the container with a fresh connect
        raise SystemExit(0)

    me = await client.get_me()
    _state["connected"] = True
    _state["status"] = "running"
    log.info("Telegram connected ✓  account: %s (%s)", me.first_name, me.username)

    # Resolve channel entities before registering handlers.
    # Telethon cannot resolve numeric string IDs (e.g. "-1003320992862") unless
    # the entity is cached. Passing the int form to get_entity() populates the
    # cache, and we then register against the resolved entity object to avoid
    # "Cannot find any entity" errors on incoming updates.
    resolved: list[tuple[str, object]] = []
    for ch_str in CHANNELS:
        try:
            ch_id: int | str = int(ch_str) if ch_str.lstrip("-").isdigit() else ch_str
            entity = await client.get_entity(ch_id)
            resolved.append((ch_str, entity))
            name = getattr(entity, "title", None) or getattr(entity, "username", None) or ch_str
            _state["channel_names"][ch_str] = name
            log.info("Channel resolved: %s → %r (id=%s)", ch_str, name, getattr(entity, "id", "?"))
        except Exception as exc:
            log.error("Cannot resolve channel '%s': %s — skipping", ch_str, exc)

    if not resolved:
        log.error("No channels could be resolved — check vault_telegram_watch_channels and account membership")
        _state["status"] = "no_channels"
        await asyncio.sleep(60)
        raise SystemExit(1)

    for ch_str, entity in resolved:
        @client.on(events.NewMessage(chats=entity))
        async def _handler(event, _ch=ch_str):
            try:
                await handle_message(event, _ch)
            except Exception as exc:
                log.error("handler error [%s]: %s", _ch, exc, exc_info=True)
                _state["stats"]["errors"] += 1

    log.info("Listening on %d channel(s): %s", len(resolved), [r[0] for r in resolved])
    await client.run_until_disconnected()
    _state["connected"] = False
    _state["status"] = "disconnected"


async def main():
    import uvicorn

    # Start uvicorn first so the /health endpoint is reachable even while
    # the Telethon client is connecting or showing auth_required
    config = uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
    server = uvicorn.Server(config)
    sentinel_task = asyncio.create_task(run_sentinel())

    try:
        await asyncio.gather(sentinel_task, server.serve())
    except (asyncio.CancelledError, SystemExit):
        pass
    finally:
        if _telethon_client:
            await _telethon_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
