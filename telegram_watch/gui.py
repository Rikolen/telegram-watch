"""Local GUI for editing tgwatch config."""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    ConfigError,
    MAX_CONTROL_GROUPS,
    MAX_TARGET_GROUPS,
    MAX_USERS_PER_TARGET,
    load_config,
)
from .migration import migrate_config

try:  # pragma: no cover - Python 3.11+ always hits first branch
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

logger = logging.getLogger(__name__)

KEEP_SECRET = "********"

_TIMEZONE_PRESET_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("协调世界时 UTC", "UTC"),
    ("中国 - 上海", "Asia/Shanghai"),
    ("中国 - 香港", "Asia/Hong_Kong"),
    ("日本 - 东京", "Asia/Tokyo"),
    # Include a second Japan option when supported by the local tz database.
    ("日本 - 大阪", "Asia/Osaka"),
    ("韩国 - 首尔", "Asia/Seoul"),
    ("美国 - 东部 (纽约)", "America/New_York"),
    ("美国 - 中部 (芝加哥)", "America/Chicago"),
    ("美国 - 太平洋 (洛杉矶)", "America/Los_Angeles"),
    ("欧洲 - 英国 (伦敦)", "Europe/London"),
    ("欧洲 - 巴黎", "Europe/Paris"),
    ("欧洲 - 柏林", "Europe/Berlin"),
    ("欧洲 - 马德里", "Europe/Madrid"),
    ("欧洲 - 罗马", "Europe/Rome"),
)


def _build_timezone_presets() -> list[dict[str, str]]:
    presets: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, value in _TIMEZONE_PRESET_CANDIDATES:
        if value in seen:
            continue
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            continue
        presets.append({"label": label, "value": value})
        seen.add(value)
    return presets

_TIME_FORMAT_UNITS: dict[str, list[dict[str, str]]] = {
    "year": [
        {"label": "四位数 (2026)", "value": "%Y"},
        {"label": "两位数 (26)", "value": "%y"},
    ],
    "month": [
        {"label": "补零 (01)", "value": "%m"},
        {"label": "不补零 (1)", "value": "%-m"},
        {"label": "缩写 (Jan)", "value": "%b"},
        {"label": "全称 (January)", "value": "%B"},
    ],
    "day": [
        {"label": "补零 (01)", "value": "%d"},
        {"label": "不补零 (1)", "value": "%-d"},
    ],
    "hour": [
        {"label": "24小时制补零 (14)", "value": "%H"},
        {"label": "12小时制补零 (02)", "value": "%I"},
        {"label": "24小时制不补零 (2)", "value": "%-H"},
    ],
    "minute": [
        {"label": "Zero-padded (05)", "value": "%M"},
    ],
    "second": [
        {"label": "Zero-padded (09)", "value": "%S"},
    ],
    "timezone": [
        {"label": "缩写 (CST)", "value": "%Z"},
        {"label": "偏移量 (+0800)", "value": "%z"},
    ],
    "date_separator": [
        {"label": "点号 (.)", "value": "."},
        {"label": "短横线 (-)", "value": "-"},
        {"label": "斜杠 (/)", "value": "/"},
    ],
}

_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>TG 监控面板</title>
    <link rel="stylesheet" href="/app.css?v=20260323" />
  </head>
  <body>
    <div id="app"></div>
    <script src="/app.js?v=20260323"></script>
  </body>
</html>
"""

_CSS = """
:root {
  color-scheme: light;
  --bg: #f7f3ef;
  --panel: #ffffff;
  --ink: #1e1b16;
  --muted: #6b6158;
  --accent: #1b6f5a;
  --accent-2: #b4682d;
  --danger: #b83c3c;
  --border: #e4dcd2;
  --shadow: 0 20px 45px rgba(42, 32, 20, 0.12);
  --radius: 18px;
  --mono: "SF Mono", "JetBrains Mono", "Fira Code", monospace;
  --sans: "Avenir Next", "Avenir", "Helvetica Neue", "Segoe UI", sans-serif;
  --log-lines: 12;
  --log-lines-collapsed: 2;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: var(--sans);
  background: radial-gradient(circle at 20% 20%, #fef7ef 0%, #f6efe7 40%, #efe6db 100%);
  color: var(--ink);
}

@keyframes rise {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

#app {
  max-width: 1100px;
  margin: 48px auto 80px;
  padding: 0 24px;
}

.header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 32px;
  animation: rise 0.6s ease both;
}

.header h1 {
  font-size: 28px;
  margin: 0;
  letter-spacing: -0.02em;
}

.header p {
  margin: 4px 0 0;
  color: var(--muted);
}

.hero {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--accent);
  font-weight: 600;
}

.actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.button {
  background: var(--accent);
  color: #fff;
  border: none;
  padding: 12px 18px;
  border-radius: 999px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 8px 20px rgba(27, 111, 90, 0.25);
}

.button.secondary {
  background: #fff;
  color: var(--accent);
  border: 1px solid var(--border);
  box-shadow: none;
}

.button.danger {
  background: var(--danger);
  color: #fff;
}

.button[disabled] {
  opacity: 0.5;
  cursor: not-allowed;
}

.section {
  background: var(--panel);
  border-radius: var(--radius);
  padding: 24px;
  margin-bottom: 24px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
  animation: rise 0.6s ease both;
}

.section h2 {
  margin: 0 0 12px;
  font-size: 20px;
}

.section p {
  margin: 0 0 16px;
  color: var(--muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field label {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
}

.checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ink);
  text-transform: none;
  letter-spacing: 0;
}

.checkbox input {
  margin: 0;
}

.field input,
.field select {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--border);
  font-size: 14px;
}

.field small {
  color: var(--muted);
}

.card-list {
  display: grid;
  gap: 16px;
}

.card {
  border-radius: 16px;
  border: 1px solid var(--border);
  padding: 16px;
  background: #fffaf4;
}

.card header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.card header h3 {
  margin: 0;
  font-size: 16px;
}

.inline-actions {
  display: flex;
  gap: 8px;
}

.list {
  display: grid;
  gap: 10px;
}

.list-row {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 8px;
  align-items: center;
}

.list-row input {
  width: 100%;
}

.list-row .button {
  padding: 8px 12px;
  font-size: 12px;
}

.notice {
  padding: 12px 14px;
  border-radius: 12px;
  background: #fef1e9;
  color: var(--accent-2);
  border: 1px dashed rgba(180, 104, 45, 0.3);
  margin-bottom: 16px;
}

.notice .checkbox {
  margin-top: 10px;
}

.error {
  padding: 12px 14px;
  border-radius: 12px;
  background: #fdecea;
  color: var(--danger);
  border: 1px solid rgba(184, 60, 60, 0.2);
  margin-bottom: 16px;
}

.lock-banner {
  padding: 18px;
  border-radius: 16px;
  border: 2px solid var(--danger);
  background: #fdecea;
  color: var(--danger);
  font-weight: 700;
  font-size: 20px;
  margin-bottom: 20px;
}

.lock-banner p {
  margin: 8px 0 0;
  font-size: 14px;
  font-weight: 500;
  color: #7f1d1d;
}

.status {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
}

.log-box {
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.4;
  background: #1b1916;
  color: #f5f1ec;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid rgba(27, 25, 22, 0.2);
  height: calc(var(--log-lines) * 1.4em + 24px);
  overflow-y: auto;
  white-space: pre-wrap;
}

.log-box.empty {
  background: #f3eee8;
  color: var(--muted);
  height: calc(var(--log-lines-collapsed) * 1.4em + 24px);
}

.runner-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
}

.runner-footnote {
  margin-top: 12px;
  font-size: 12px;
  color: var(--muted);
}

@media (max-width: 720px) {
  .list-row {
    grid-template-columns: 1fr;
  }
  .inline-actions {
    flex-direction: column;
    align-items: flex-start;
  }
}
"""

_JS = """
const state = {
  data: null,
  errors: [],
  status: "",
  runner: null,
  runnerMessage: "",
  runnerSince: "2h",
  runnerTarget: "",
  runnerPush: false,
  runnerRetentionConfirmed: false,
  runnerRetentionPrompt: false,
  runnerLoading: false,
  locked: false,
  lockMessage: "",
  migrationStatus: "",
  timeFormatParts: null,
  timeFormatCustom: false
};
const runnerDefaults = {
  running: false,
  pid: null,
  run_log: "",
  once_log: "",
  status: "",
  session_ready: true,
  requires_retention_confirm: false,
  retention_days: 30
};
const keepSecret = "********";
const LOG_MAX_LINES = 200;

const limitText = (limits) => `限制：最多 ${limits.maxTargets} 个群组，每组 ${limits.maxUsersPerTarget} 个用户，${limits.maxControlGroups} 个控制组。`;

const blankTarget = () => ({
  name: "",
  target_chat_id: "",
  summary_interval_minutes: "",
  control_group: "",
  tracked_users: [{ id: "", alias: "" }]
});

const blankControlGroup = () => ({
  key: "",
  control_chat_id: "",
  is_forum: false,
  topic_routing_enabled: false,
  skip_html_report: false,
  topic_target_map: [{ user_key: "", target_chat_id: "", user_id: "", topic_id: "" }]
});

const buildTargetUsers = (targets) => {
  const users = [];
  targets.forEach((target, tIdx) => {
    const name = target.name || `group-${tIdx + 1}`;
    const targetChatId = String(target.target_chat_id || "").trim();
    if (!targetChatId) return;
    (target.tracked_users || []).forEach((user) => {
      const id = String(user.id || "").trim();
      if (!id) return;
      const alias = String(user.alias || "").trim();
      const label = `${name} - ${id}${alias ? ` (${alias})` : ""}`;
      const key = `${targetChatId}|${id}`;
      users.push({ key, user_id: id, target_chat_id: targetChatId, label, targetName: name });
    });
  });
  return users;
};

const entryKey = (entry) => {
  if (!entry) return "";
  const key = String(entry.user_key || "").trim();
  if (key) return key;
  const targetChatId = String(entry.target_chat_id || "").trim();
  const userId = String(entry.user_id || "").trim();
  if (targetChatId && userId) return `${targetChatId}|${userId}`;
  return "";
};

const collectSelectedUsers = (controlGroups) => {
  const selected = new Set();
  controlGroups.forEach((group) => {
    if (!group.topic_routing_enabled) return;
    (group.topic_target_map || []).forEach((entry) => {
      const value = entryKey(entry);
      if (value) selected.add(value);
    });
  });
  return selected;
};

const mapTargetsToControl = (targets, controlGroups, key) => {
  if (controlGroups.length === 1) {
    return targets.map((target, idx) => target.name || `group-${idx + 1}`);
  }
  return targets
    .filter((target) => String(target.control_group || "") === String(key || ""))
    .map((target, idx) => target.name || `group-${idx + 1}`);
};

const buildUserOptions = (targetUsers, selectedUsers, currentValue) => {
  const available = new Set(selectedUsers);
  if (currentValue) {
    available.delete(currentValue);
  }
  const options = [];
  const seen = new Set();
  targetUsers.forEach((user) => {
    if (available.has(user.key)) return;
    const label = user.label;
    const value = user.key;
    if (seen.has(value)) return;
    seen.add(value);
    options.push(`<option value="${value}">${label}</option>`);
  });
  if (currentValue && !targetUsers.some((user) => user.key === currentValue)) {
    options.unshift(`<option value="${currentValue}">未知用户 (${currentValue})</option>`);
  }
  if (!options.length) {
    options.push(`<option value="">没有可用的用户</option>`);
  } else {
    options.unshift(`<option value="">选择用户</option>`);
  }
  return options.join("");
};

const buildTimezoneOptions = (presets, currentValue) => {
  const selected = String(currentValue || "UTC").trim() || "UTC";
  const options = [];
  const seen = new Set();
  (presets || []).forEach((entry) => {
    if (!entry || !entry.value) return;
    const value = String(entry.value);
    const label = String(entry.label || entry.value);
    if (seen.has(value)) return;
    seen.add(value);
    options.push(
      `<option value="${value}" ${value === selected ? "selected" : ""}>${label} (${value})</option>`
    );
  });
  if (!seen.has(selected)) {
    options.unshift(
      `<option value="${selected}" selected>自定义（保留现有）- ${selected}</option>`
    );
  }
  if (!options.length) {
    options.push(`<option value="UTC" ${selected === "UTC" ? "selected" : ""}>UTC (UTC)</option>`);
  }
  return options.join("");
};

// --- Time Format Builder helpers ---

const _TF_KNOWN_CODES = {
  year: ["%Y", "%y"],
  month: ["%m", "%-m", "%b", "%B"],
  day: ["%d", "%-d"],
  hour: ["%H", "%I", "%-H"],
  minute: ["%M"],
  second: ["%S"],
};
const _TF_DATE_SEPS = [".", "-", "/"];
const _TF_TZ_CODES = ["%Z", "%z"];

function parseTimeFormat(fmt) {
  if (!fmt || typeof fmt !== "string") return null;
  let s = fmt.trim();
  let tz = "";
  for (const code of _TF_TZ_CODES) {
    const suffix = " (" + code + ")";
    if (s.endsWith(suffix)) {
      tz = code;
      s = s.slice(0, -suffix.length);
      break;
    }
  }
  // Split into date part and time part at the space boundary.
  // Heuristic: time part starts with a known hour code.
  let datePart = "";
  let timePart = "";
  const spaceIdx = s.indexOf(" ");
  if (spaceIdx === -1) {
    // Single segment — decide if it is date or time.
    if (_TF_KNOWN_CODES.hour.some((c) => s.startsWith(c))) {
      timePart = s;
    } else {
      datePart = s;
    }
  } else {
    datePart = s.slice(0, spaceIdx);
    timePart = s.slice(spaceIdx + 1);
  }

  // Parse date
  let year = "", month = "", day = "", dateSep = ".";
  if (datePart) {
    let sep = "";
    for (const candidate of _TF_DATE_SEPS) {
      if (datePart.includes(candidate)) { sep = candidate; break; }
    }
    dateSep = sep || ".";
    const dateCodes = sep ? datePart.split(sep) : [datePart];
    // Identify each code
    const identified = [];
    for (const code of dateCodes) {
      if (!code) continue;
      let found = false;
      for (const [unit, codes] of Object.entries(_TF_KNOWN_CODES)) {
        if (["year", "month", "day"].includes(unit) && codes.includes(code)) {
          identified.push({ unit, code });
          found = true;
          break;
        }
      }
      if (!found) return null; // unrecognised code
    }
    for (const item of identified) {
      if (item.unit === "year") year = item.code;
      else if (item.unit === "month") month = item.code;
      else if (item.unit === "day") day = item.code;
    }
  }

  // Parse time
  let hour = "", minute = "", second = "";
  if (timePart) {
    const timeCodes = timePart.split(":");
    const tIdentified = [];
    for (const code of timeCodes) {
      if (!code) continue;
      let found = false;
      for (const [unit, codes] of Object.entries(_TF_KNOWN_CODES)) {
        if (["hour", "minute", "second"].includes(unit) && codes.includes(code)) {
          tIdentified.push({ unit, code });
          found = true;
          break;
        }
      }
      if (!found) return null;
    }
    for (const item of tIdentified) {
      if (item.unit === "hour") hour = item.code;
      else if (item.unit === "minute") minute = item.code;
      else if (item.unit === "second") second = item.code;
    }
  }

  return { year, month, day, dateSep, hour, minute, second, timezone: tz };
}

function composeTimeFormat(parts) {
  const dateCodes = [parts.year, parts.month, parts.day].filter(Boolean);
  const timeCodes = [parts.hour, parts.minute, parts.second].filter(Boolean);
  let result = "";
  if (dateCodes.length) {
    result = dateCodes.join(parts.dateSep || ".");
  }
  if (timeCodes.length) {
    if (result) result += " ";
    result += timeCodes.join(":");
  }
  if (parts.timezone) {
    if (result) result += " ";
    result += "(" + parts.timezone + ")";
  }
  return result || "%Y.%m.%d %H:%M:%S (%Z)";
}

function buildTimeFormatDropdown(unitName, presets, currentValue, fieldId) {
  const options = ['<option value="">不显示</option>'];
  (presets || []).forEach((entry) => {
    const sel = entry.value === currentValue ? "selected" : "";
    options.push('<option value="' + entry.value + '" ' + sel + '>' + entry.label + "</option>");
  });
  return '<select id="' + fieldId + '" data-tf-unit="' + unitName + '">' + options.join("") + "</select>";
}

function timeFormatPreview(parts) {
  const sample = {
    "%Y": "2026", "%y": "26",
    "%m": "01", "%-m": "1", "%b": "Jan", "%B": "January",
    "%d": "05", "%-d": "5",
    "%H": "14", "%I": "02", "%-H": "2",
    "%M": "05", "%S": "09",
    "%Z": "CST", "%z": "+0800"
  };
  const fmt = composeTimeFormat(parts);
  let result = fmt;
  // Replace longest codes first to avoid partial matches (e.g. %-m before %m).
  const codes = Object.keys(sample).sort((a, b) => b.length - a.length);
  for (const code of codes) {
    result = result.split(code).join(sample[code]);
  }
  return result;
}

const runnerStatusText = (runner) => {
  if (!runner) return "正在检查状态...";
  if (runner.running) {
    return runner.pid ? `运行中 (PID ${runner.pid})` : "运行中";
  }
  return "未运行";
};

const runnerMessageText = () => {
  if (state.runner && state.runner.status) return state.runner.status;
  if (state.runnerMessage) return state.runnerMessage;
  return "";
};

const trimLogLines = (text) => {
  if (!text) return "";
  const lines = text.split("\\n");
  if (lines.length <= LOG_MAX_LINES) return text;
  return lines.slice(lines.length - LOG_MAX_LINES).join("\\n");
};

function updateRunnerUI() {
  const runner = state.runner || runnerDefaults;
  const statusEl = document.getElementById("runner-status");
  if (!statusEl) return;
  statusEl.textContent = runnerStatusText(runner);

  const runLogEl = document.getElementById("run-log");
  if (runLogEl) {
    const runLogText = runner.running ? trimLogLines(runner.run_log || "") : "";
    if (runner.running && runLogText) {
      runLogEl.textContent = runLogText;
      runLogEl.classList.remove("empty");
    } else if (runner.running) {
      runLogEl.textContent = "等待日志...";
      runLogEl.classList.add("empty");
    } else {
      runLogEl.textContent = "没有正在运行的任务";
      runLogEl.classList.add("empty");
    }
  }

  const onceLogEl = document.getElementById("once-log");
  if (onceLogEl) {
    const onceLogText = trimLogLines(runner.once_log || "");
    if (onceLogText) {
      onceLogEl.textContent = onceLogText;
      onceLogEl.classList.remove("empty");
    } else {
      onceLogEl.textContent = "没有最近的单次运行记录";
      onceLogEl.classList.add("empty");
    }
  }

  const messageEl = document.getElementById("runner-message");
  const message = runnerMessageText();
  if (messageEl) {
    if (message) {
      messageEl.textContent = message;
      messageEl.style.display = "block";
    } else {
      messageEl.textContent = "";
      messageEl.style.display = "none";
    }
  }

  const runButton = document.querySelector('[data-action="run-daemon"]');
  if (runButton) {
    const sessionReady = Boolean(runner.session_ready);
    runButton.disabled = Boolean(runner.running || !sessionReady);
  }
  const stopButton = document.querySelector('[data-action="run-daemon-stop"]');
  if (stopButton) {
    stopButton.disabled = !Boolean(runner.running);
  }
  const onceButton = document.querySelector('[data-action="run-once"]');
  if (onceButton) {
    onceButton.disabled = !Boolean(runner.session_ready);
  }
  const retentionConfirmButton = document.querySelector('[data-action="run-daemon-confirm"]');
  if (retentionConfirmButton) {
    retentionConfirmButton.disabled = !Boolean(state.runnerRetentionConfirmed);
  }
  const retentionCheckbox = document.getElementById("run-retention-confirm");
  if (retentionCheckbox) {
    retentionCheckbox.checked = Boolean(state.runnerRetentionConfirmed);
  }
}

function applyLockState() {
  const locked = Boolean(state.locked);
  const banner = document.getElementById("lock-banner");
  if (banner) {
    banner.style.display = locked ? "block" : "none";
  }
  document.querySelectorAll("#app input, #app select, #app button, #app textarea").forEach((el) => {
    if (el.dataset.allowLocked) return;
    el.disabled = locked;
  });
}

async function loadRunnerStatus() {
  if (state.runnerLoading) return;
  state.runnerLoading = true;
  try {
    const res = await fetch("/api/runner/status");
    const payload = await res.json();
    state.runner = payload;
    if (!payload.requires_retention_confirm) {
      state.runnerRetentionConfirmed = false;
      state.runnerRetentionPrompt = false;
    }
    if (payload.running) {
      state.runnerRetentionConfirmed = false;
      state.runnerRetentionPrompt = false;
    }
    updateRunnerUI();
  } catch (err) {
    state.runnerMessage = "无法获取运行状态";
    updateRunnerUI();
  } finally {
    state.runnerLoading = false;
  }
}

async function startRun() {
  const runner = state.runner || runnerDefaults;
  if (!runner.session_ready) {
    state.runnerMessage = "未找到会话文件，请先在终端完成一次登录。";
    updateRunnerUI();
    return;
  }
  if (runner.requires_retention_confirm) {
    if (!state.runnerRetentionPrompt) {
      state.runnerRetentionPrompt = true;
      state.runnerRetentionConfirmed = false;
      state.runnerMessage = "";
      render();
      updateRunnerUI();
      return;
    }
    if (!state.runnerRetentionConfirmed) {
      state.runnerMessage = `请先确认 retention_days=${runner.retention_days} 后再启动守护进程。`;
      updateRunnerUI();
      return;
    }
  }
  await startRunConfirmed();
}

async function startRunConfirmed() {
  const runner = state.runner || runnerDefaults;
  if (runner.requires_retention_confirm && !state.runnerRetentionConfirmed) {
    return;
  }
  try {
    state.runnerMessage = "";
    updateRunnerUI();
    const res = await fetch("/api/runner/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_retention: state.runnerRetentionConfirmed })
    });
    const payload = await res.json();
    state.runnerMessage = payload.status || "";
    if (payload.ok) {
      state.runnerRetentionPrompt = false;
      state.runnerRetentionConfirmed = false;
      render();
    }
    await loadRunnerStatus();
    updateRunnerUI();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.runnerMessage = `启动守护进程失败：${message}`;
    updateRunnerUI();
  }
}

async function stopRun() {
  try {
    state.runnerMessage = "";
    updateRunnerUI();
    const res = await fetch("/api/runner/stop", { method: "POST" });
    const payload = await res.json();
    state.runnerMessage = payload.status || "";
    await loadRunnerStatus();
    updateRunnerUI();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.runnerMessage = `停止守护进程失败：${message}`;
    updateRunnerUI();
  }
}

async function startOnce() {
  const sinceInput = document.getElementById("once-since");
  const targetInput = document.getElementById("once-target");
  const pushInput = document.getElementById("once-push");
  const since = sinceInput ? sinceInput.value.trim() : "";
  const target = targetInput ? targetInput.value.trim() : state.runnerTarget;
  const push = pushInput ? pushInput.checked : state.runnerPush;
  if (!since) {
    state.runnerMessage = "请输入有效的时间窗口（如 2h）。";
    updateRunnerUI();
    return;
  }
  try {
    state.runnerMessage = "";
    updateRunnerUI();
    const res = await fetch("/api/runner/once", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ since, target, push })
    });
    const payload = await res.json();
    state.runnerMessage = payload.status || "";
    await loadRunnerStatus();
    updateRunnerUI();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.runnerMessage = `启动单次运行失败：${message}`;
    updateRunnerUI();
  }
}

function startRunnerPolling() {
  loadRunnerStatus();
  window.setInterval(loadRunnerStatus, 2000);
}

async function stopGui() {
  try {
    const res = await fetch("/api/gui/stop", { method: "POST" });
    const payload = await res.json();
    const app = document.getElementById("app");
    const message = payload && payload.status ? payload.status : "面板已停止";
    app.innerHTML = `<div class="section"><h2>面板已停止</h2><p>${message}</p></div>`;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.status = `停止面板失败：${message}`;
    render();
  }
}

async function migrateConfig() {
  try {
    const res = await fetch("/api/config/migrate", { method: "POST" });
    const payload = await res.json();
    state.migrationStatus = payload.status || "";
    await loadConfig();
    render();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.migrationStatus = `迁移失败：${message}`;
    render();
  }
}

function setByPath(obj, path, value) {
  const parts = path.split(".");
  let cursor = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const key = parts[i];
    cursor = cursor[key];
  }
  cursor[parts[parts.length - 1]] = value;
}

function render() {
  const app = document.getElementById("app");
  if (!state.data) {
    const errorBlock = state.errors.length
      ? `<div class="error"><strong>无法加载面板</strong><ul>${state.errors
          .map((e) => `<li>${e}</li>`)
          .join("")}</ul></div>`
      : "";
    app.innerHTML = `<div class="section"><h2>加载中...</h2>${errorBlock}</div>`;
    return;
  }
  const data = state.data;
  const limits = data.limits;
  const targets = data.targets;
  const controlGroups = data.control_groups;
  const targetUsers = buildTargetUsers(targets);
  const selectedUsers = collectSelectedUsers(controlGroups);
  const timezonePresets = data.reporting_timezone_presets || [];
  const selectedOnceTarget = state.runnerTarget || "";
  const oncePushChecked = state.runnerPush;

  const errorBlock = state.errors.length
    ? `<div class="error"><strong>验证问题</strong><ul>${state.errors.map(e => `<li>${e}</li>`).join("")}</ul></div>`
    : "";

  const noticeParts = [];
  if (state.status) noticeParts.push(state.status);
  if (state.migrationStatus) noticeParts.push(state.migrationStatus);
  const statusBlock = noticeParts.length ? `<div class="notice">${noticeParts.join("<br />")}</div>` : "";
  const lockBanner = state.locked
    ? `<div class="lock-banner" id="lock-banner">
        配置已锁定
        <p>${state.lockMessage || "当前配置已过期或无效，请重写 config.toml 并重新加载面板。"}</p>
        <div style="margin-top:12px;">
          <button class="button danger" data-action="migrate-config" data-allow-locked="true">迁移配置</button>
        </div>
      </div>`
    : "";

  const controlOptions = controlGroups
    .map((group, idx) => `<option value="${group.key}">${group.key || `control-${idx + 1}`}</option>`)
    .join("");
  const defaultControlLabel = controlGroups.length === 1
    ? `default (${controlGroups[0].key || "control-1"})`
    : "Select";

  const runner = state.runner || runnerDefaults;
  const runnerMessage = runnerMessageText();
  const runLog = runner.running ? runner.run_log : "";
  const onceLog = runner.once_log || "";
  const sessionBanner = !runner.session_ready
    ? `<div class="lock-banner" style="margin-bottom:16px;">
        运行前需要先登录会话
        <p>请在终端运行一次以完成登录：<code>python -m tgwatch run --config config.toml</code></p>
      </div>`
    : "";
  const retentionNotice = runner.requires_retention_confirm && state.runnerRetentionPrompt
    ? `<div class="notice">
        <strong>需要确认数据保留设置：</strong>
        retention_days 已设为 ${runner.retention_days}，请在启动守护进程前确认。
        <label class="checkbox">
          <input type="checkbox" id="run-retention-confirm" ${state.runnerRetentionConfirmed ? "checked" : ""} />
          我了解较长的保留时间可能会占用大量磁盘空间。
        </label>
        <div class="actions" style="margin-top:10px;">
          <button class="button secondary" data-action="run-daemon-confirm" ${state.runnerRetentionConfirmed ? "" : "disabled"}>确认并启动</button>
          <button class="button secondary" data-action="run-daemon-cancel">取消</button>
        </div>
      </div>`
    : "";

  app.innerHTML = `
    <div class="header">
      <div class="hero">
        <span class="badge">本地配置器</span>
        <h1>TG 监控面板</h1>
        <p>配置多群组监控和消息路由，无需手动编辑配置文件。</p>
        <div class="status">${limitText(limits)}</div>
      </div>
      <div class="actions">
        <button class="button" data-action="save">保存配置</button>
        <button class="button secondary" data-action="reload" data-allow-locked="true">重新加载</button>
      </div>
    </div>
    ${statusBlock}
    ${errorBlock}
    ${lockBanner}

    <section class="section" id="runner-section">
      <h2>运行控制</h2>
      <p>执行单次抓取或在后台持续运行监控。关闭浏览器不会停止正在运行的守护进程。</p>
      ${sessionBanner}
      ${retentionNotice}
      <div class="runner-grid">
        <div class="field">
          <label>时间窗口</label>
          <input id="once-since" value="${state.runnerSince}" placeholder="2h" />
          <small>示例：10m、2h、2026-02-01T10:30Z</small>
        </div>
        <div class="field">
          <label>目标群组</label>
          <select id="once-target">
            ${(() => {
              const options = [];
              options.push(
                `<option value="" ${selectedOnceTarget === "" ? "selected" : ""}>全部目标</option>`
              );
              targets.forEach((target, idx) => {
                const label = target.name || `group-${idx + 1}`;
                const value = String(target.target_chat_id || "").trim();
                if (value) {
                  options.push(
                    `<option value="${value}" ${value === selectedOnceTarget ? "selected" : ""}>${label} (${value})</option>`
                  );
                } else {
                  options.push(
                    `<option value="${label}" ${label === selectedOnceTarget ? "selected" : ""}>${label} (name)</option>`
                  );
                }
              });
              return options.join("");
            })()}
          </select>
          <small>选择单个目标，或保持”全部目标”。</small>
        </div>
        <div class="field">
          <label>推送报告</label>
          <label class="checkbox">
            <input type="checkbox" id="once-push" ${oncePushChecked ? "checked" : ""} />
            推送到控制群
          </label>
          <small>默认关闭；启用后将推送单次运行的报告。</small>
        </div>
        <div class="field">
          <label>守护进程状态</label>
          <div class="status" id="runner-status">${runnerStatusText(runner)}</div>
        </div>
      </div>
      <div class="actions" style="margin-top:16px;">
        <button class="button" data-action="run-once">单次运行</button>
        <button class="button secondary" data-action="run-daemon">启动守护进程</button>
        <button class="button secondary" data-action="run-daemon-stop" disabled>停止守护进程</button>
        <button class="button danger" data-action="stop-gui" data-allow-locked="true">停止面板</button>
      </div>
      <div class="runner-footnote">停止面板不会影响正在运行的守护进程。</div>
      <div class="notice" id="runner-message" style="${runnerMessage ? "" : "display:none;"}">${runnerMessage}</div>
      <div class="grid" style="margin-top:16px;">
        <div>
          <div class="status">运行日志（实时）</div>
          <pre class="log-box ${runner.running && runLog ? "" : "empty"}" id="run-log">${runner.running ? (runLog || "等待日志...") : "没有正在运行的任务"}</pre>
        </div>
        <div>
          <div class="status">单次运行日志</div>
          <pre class="log-box ${onceLog ? "" : "empty"}" id="once-log">${onceLog || "没有最近的单次运行记录"}</pre>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Telegram 凭证</h2>
      <p>仅本地使用。API Hash 已脱敏并保存在磁盘上。</p>
      <div class="grid">
        <div class="field">
          <label>API ID</label>
          <input data-field="telegram.api_id" value="${data.telegram.api_id}" placeholder="123456" />
        </div>
        <div class="field">
          <label>API Hash</label>
          <input type="password" data-field="telegram.api_hash" value="${data.telegram.api_hash}" placeholder="${keepSecret}" />
        </div>
        <div class="field">
          <label>会话文件</label>
          <input data-field="telegram.session_file" value="${data.telegram.session_file}" placeholder="data/tgwatch.session" />
        </div>
      </div>
      <div class="field" style="margin-top:16px;">
        <label><input type="checkbox" data-field="sender.enabled" ${data.sender.enabled ? "checked" : ""}/> 启用发送者账户（可选）</label>
      </div>
      ${data.sender.enabled ? `
      <div class="grid" style="margin-top:12px;">
        <div class="field">
          <label>发送者会话文件</label>
          <input data-field="sender.session_file" value="${data.sender.session_file}" placeholder="data/tgwatch_sender.session" />
        </div>
      </div>` : ""}
    </section>

    <section class="section">
      <h2>监控目标</h2>
      <p>每个目标代表一个被监控的群组或频道。</p>
      <div class="card-list">
        ${targets.map((target, idx) => `
        <div class="card">
          <header>
            <h3>Group ${idx + 1}</h3>
            <div class="inline-actions">
              <button class="button secondary" data-action="add-user" data-target-index="${idx}" ${target.tracked_users.length >= limits.maxUsersPerTarget ? "disabled" : ""}>添加用户</button>
              <button class="button danger" data-action="remove-target" data-target-index="${idx}">移除群组</button>
            </div>
          </header>
          <div class="grid">
            <div class="field">
              <label>名称</label>
              <input data-field="targets.${idx}.name" value="${target.name}" placeholder="group-${idx + 1}" />
            </div>
            <div class="field">
              <label>目标群组 ID</label>
              <input data-field="targets.${idx}.target_chat_id" value="${target.target_chat_id}" placeholder="-100..." />
            </div>
            <div class="field">
              <label>报告间隔（分钟）</label>
              <input data-field="targets.${idx}.summary_interval_minutes" value="${target.summary_interval_minutes}" placeholder="optional" />
            </div>
            <div class="field">
              <label>控制组</label>
              <select data-field="targets.${idx}.control_group">
                <option value="">${defaultControlLabel}</option>
                ${controlOptions}
              </select>
            </div>
          </div>
          <div class="list" style="margin-top:16px;">
            ${target.tracked_users.map((user, uidx) => `
            <div class="list-row">
              <input data-field="targets.${idx}.tracked_users.${uidx}.id" value="${user.id}" placeholder="用户 ID" />
              <input data-field="targets.${idx}.tracked_users.${uidx}.alias" value="${user.alias}" placeholder="别名（可选）" />
              <button class="button secondary" data-action="remove-user" data-target-index="${idx}" data-user-index="${uidx}">移除</button>
            </div>`).join("")}
          </div>
        </div>`).join("")}
      </div>
      <div style="margin-top:16px;">
        <button class="button secondary" data-action="add-target" ${targets.length >= limits.maxTargets ? "disabled" : ""}>添加群组</button>
      </div>
    </section>

    <section class="section">
      <h2>控制组</h2>
      <p>汇总报告和命令的接收群组。</p>
      <div class="card-list">
        ${controlGroups.map((group, idx) => `
        <div class="card">
          <header>
            <h3>Control ${idx + 1}</h3>
            <div class="inline-actions">
              <button class="button danger" data-action="remove-control" data-control-index="${idx}">移除</button>
            </div>
          </header>
          <div class="grid">
            <div class="field">
              <label>标识</label>
              <input data-field="control_groups.${idx}.key" value="${group.key}" placeholder="main" />
            </div>
            <div class="field">
              <label>控制群 ID</label>
              <input data-field="control_groups.${idx}.control_chat_id" value="${group.control_chat_id}" placeholder="-100..." />
            </div>
            <div class="field">
              <label>论坛模式</label>
              <select data-field="control_groups.${idx}.is_forum">
                <option value="false" ${group.is_forum ? "" : "selected"}>false</option>
                <option value="true" ${group.is_forum ? "selected" : ""}>true</option>
              </select>
            </div>
            <div class="field">
              <label>话题路由</label>
              <select data-field="control_groups.${idx}.topic_routing_enabled">
                <option value="false" ${group.topic_routing_enabled ? "" : "selected"}>false</option>
                <option value="true" ${group.topic_routing_enabled ? "selected" : ""}>true</option>
              </select>
            </div>
            <div class="field">
              <label>跳过 HTML 报告</label>
              <select data-field="control_groups.${idx}.skip_html_report">
                <option value="false" ${group.skip_html_report ? "" : "selected"}>false</option>
                <option value="true" ${group.skip_html_report ? "selected" : ""}>true</option>
              </select>
            </div>
          </div>
          <div class="status" style="margin-top:12px;">
            已映射的目标： ${(() => {
              const mapped = mapTargetsToControl(targets, controlGroups, group.key);
              return mapped.length ? mapped.join(", ") : "暂无";
            })()}
          </div>
          ${group.topic_routing_enabled ? `
          <div class="list" style="margin-top:16px;">
            ${group.topic_target_map.map((entry, eidx) => `
            <div class="list-row">
              <select data-field="control_groups.${idx}.topic_target_map.${eidx}.user_key">
                ${buildUserOptions(targetUsers, selectedUsers, entryKey(entry))}
              </select>
              <input data-field="control_groups.${idx}.topic_target_map.${eidx}.topic_id" value="${entry.topic_id}" placeholder="话题 ID" />
              <button class="button secondary" data-action="remove-topic" data-control-index="${idx}" data-topic-index="${eidx}">移除</button>
            </div>`).join("")}
          </div>
          <div style="margin-top:12px;">
            <button class="button secondary" data-action="add-topic" data-control-index="${idx}">添加话题映射</button>
          </div>` : ""}
        </div>`).join("")}
      </div>
      <div style="margin-top:16px;">
        <button class="button secondary" data-action="add-control" ${controlGroups.length >= limits.maxControlGroups ? "disabled" : ""}>添加控制组</button>
      </div>
    </section>

    <section class="section">
      <h2>存储和报告</h2>
      <div class="grid">
        <div class="field">
          <label>数据库路径</label>
          <input data-field="storage.db_path" value="${data.storage.db_path}" />
        </div>
        <div class="field">
          <label>媒体目录</label>
          <input data-field="storage.media_dir" value="${data.storage.media_dir}" />
        </div>
        <div class="field">
          <label>报告目录</label>
          <input data-field="reporting.reports_dir" value="${data.reporting.reports_dir}" />
        </div>
        <div class="field">
          <label>默认汇总间隔</label>
          <input data-field="reporting.summary_interval_minutes" value="${data.reporting.summary_interval_minutes}" />
        </div>
        <div class="field">
          <label>时区</label>
          <select data-field="reporting.timezone">
            ${buildTimezoneOptions(timezonePresets, data.reporting.timezone)}
          </select>
          <small>保存为 IANA 时区字符串（如 Asia/Shanghai）。</small>
        </div>
        <div class="field">
          <label>数据保留天数</label>
          <input data-field="reporting.retention_days" value="${data.reporting.retention_days}" />
        </div>
      </div>
    </section>

    <section class="section">
      <h2>显示和通知</h2>
      <div class="grid">
        <div class="field">
          <label>显示 ID</label>
          <select data-field="display.show_ids">
            <option value="true" ${data.display.show_ids ? "selected" : ""}>true</option>
            <option value="false" ${data.display.show_ids ? "" : "selected"}>false</option>
          </select>
        </div>
        <div class="field">
          <label>Bark 推送密钥</label>
          <input data-field="notifications.bark_key" value="${data.notifications.bark_key}" placeholder="optional" />
        </div>
      </div>
      ${(() => {
        const tfUnits = data.display_time_format_units || {};
        if (state.timeFormatCustom || !state.timeFormatParts) {
          return '<div style="margin-top:16px;"><div class="field"><label>时间格式（自定义）</label><input data-field="display.time_format" value="' + (data.display.time_format || "") + '" /><small><a href="#" data-action="tf-switch-builder">切换到构建器</a></small></div></div>';
        }
        const p = state.timeFormatParts;
        const hasMultiDate = [p.year, p.month, p.day].filter(Boolean).length > 1;
        return '<div style="margin-top:16px;"><label style="font-weight:600;margin-bottom:8px;display:block;">时间格式</label>'
          + '<div class="grid">'
          + '<div class="field"><label>年</label>' + buildTimeFormatDropdown("year", tfUnits.year, p.year, "tf-year") + '</div>'
          + '<div class="field"><label>月</label>' + buildTimeFormatDropdown("month", tfUnits.month, p.month, "tf-month") + '</div>'
          + '<div class="field"><label>日</label>' + buildTimeFormatDropdown("day", tfUnits.day, p.day, "tf-day") + '</div>'
          + (hasMultiDate ? '<div class="field"><label>日期分隔符</label>' + buildTimeFormatDropdown("dateSep", tfUnits.date_separator, p.dateSep, "tf-datesep") + '</div>' : '')
          + '<div class="field"><label>时</label>' + buildTimeFormatDropdown("hour", tfUnits.hour, p.hour, "tf-hour") + '</div>'
          + '<div class="field"><label>分</label>' + buildTimeFormatDropdown("minute", tfUnits.minute, p.minute, "tf-minute") + '</div>'
          + '<div class="field"><label>秒</label>' + buildTimeFormatDropdown("second", tfUnits.second, p.second, "tf-second") + '</div>'
          + '<div class="field"><label>时区</label>' + buildTimeFormatDropdown("timezone", tfUnits.timezone, p.timezone, "tf-timezone") + '</div>'
          + '</div>'
          + '<div style="margin-top:12px;"><small>Preview: <strong style="font-family:var(--mono);color:var(--accent);">' + timeFormatPreview(p) + '</strong></small>'
          + '&nbsp;&nbsp;<small>Format: <code style="font-size:12px;color:var(--muted);">' + composeTimeFormat(p) + '</code></small></div>'
          + '<small><a href="#" data-action="tf-switch-custom">编辑原始 strftime 字符串</a></small>'
          + '</div>';
      })()}
    </section>
  `;

  document.querySelectorAll("select[data-field]").forEach((select) => {
    const field = select.dataset.field;
    const value = getByPath(state.data, field);
    select.value = String(value);
  });
  applyLockState();
}

function getByPath(obj, path) {
  return path.split(".").reduce((acc, key) => acc[key], obj);
}

function bindEvents() {
  document.addEventListener("input", (event) => {
    const target = event.target;
    if (target.id === "once-since") {
      state.runnerSince = target.value;
      return;
    }
    if (target.id === "once-target") {
      state.runnerTarget = target.value;
      return;
    }
    if (target.id === "once-push") {
      state.runnerPush = target.checked;
      return;
    }
    if (target.id === "run-retention-confirm") {
      state.runnerRetentionConfirmed = target.checked;
      updateRunnerUI();
      return;
    }
    if (!target.dataset.field) return;
    const field = target.dataset.field;
    let value = target.type === "checkbox" ? target.checked : target.value;
    if (target.tagName === "SELECT") {
      value = target.value === "true" ? true : target.value === "false" ? false : target.value;
    }
    setByPath(state.data, field, value);
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target.dataset && target.dataset.tfUnit) {
      const unit = target.dataset.tfUnit;
      if (state.timeFormatParts) {
        state.timeFormatParts[unit] = target.value;
        const composed = composeTimeFormat(state.timeFormatParts);
        state.data.display.time_format = composed;
        render();
      }
      return;
    }
    if (target.id === "once-target") {
      state.runnerTarget = target.value;
      return;
    }
    if (target.id === "once-push") {
      state.runnerPush = target.checked;
      return;
    }
    if (target.id === "run-retention-confirm") {
      state.runnerRetentionConfirmed = target.checked;
      updateRunnerUI();
      return;
    }
    if (!target.dataset.field) return;
    const field = target.dataset.field;
    if (
      field === "sender.enabled" ||
      field.startsWith("targets.") ||
      field.endsWith(".key") ||
      field.endsWith(".topic_routing_enabled") ||
      field.endsWith(".is_forum") ||
      field.endsWith(".skip_html_report") ||
      field.endsWith(".control_group") ||
      field.includes(".topic_target_map.")
    ) {
      render();
    }
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    const action = target.dataset.action;
    if (!action) return;

    if (action === "add-target") {
      state.data.targets.push(blankTarget());
      render();
      return;
    }
    if (action === "remove-target") {
      const index = Number(target.dataset.targetIndex);
      state.data.targets.splice(index, 1);
      render();
      return;
    }
    if (action === "add-user") {
      const index = Number(target.dataset.targetIndex);
      state.data.targets[index].tracked_users.push({ id: "", alias: "" });
      render();
      return;
    }
    if (action === "remove-user") {
      const tIndex = Number(target.dataset.targetIndex);
      const uIndex = Number(target.dataset.userIndex);
      state.data.targets[tIndex].tracked_users.splice(uIndex, 1);
      render();
      return;
    }
    if (action === "add-control") {
      state.data.control_groups.push(blankControlGroup());
      render();
      return;
    }
    if (action === "remove-control") {
      const index = Number(target.dataset.controlIndex);
      state.data.control_groups.splice(index, 1);
      render();
      return;
    }
    if (action === "add-topic") {
      const index = Number(target.dataset.controlIndex);
      state.data.control_groups[index].topic_target_map.push({
        user_key: "",
        target_chat_id: "",
        user_id: "",
        topic_id: ""
      });
      render();
      return;
    }
    if (action === "remove-topic") {
      const cIndex = Number(target.dataset.controlIndex);
      const tIndex = Number(target.dataset.topicIndex);
      state.data.control_groups[cIndex].topic_target_map.splice(tIndex, 1);
      render();
      return;
    }
    if (action === "save") {
      saveConfig();
      return;
    }
    if (action === "reload") {
      loadConfig();
      return;
    }
    if (action === "run-once") {
      startOnce();
      return;
    }
    if (action === "run-daemon") {
      startRun();
      return;
    }
    if (action === "run-daemon-confirm") {
      startRunConfirmed();
      return;
    }
    if (action === "run-daemon-stop") {
      stopRun();
      return;
    }
    if (action === "run-daemon-cancel") {
      state.runnerRetentionPrompt = false;
      state.runnerRetentionConfirmed = false;
      state.runnerMessage = "";
      render();
      updateRunnerUI();
      return;
    }
    if (action === "stop-gui") {
      stopGui();
      return;
    }
    if (action === "migrate-config") {
      migrateConfig();
      return;
    }
    if (action === "tf-switch-builder") {
      event.preventDefault();
      const parsed = parseTimeFormat(state.data.display.time_format || "");
      if (parsed) {
        state.timeFormatParts = parsed;
      } else {
        state.timeFormatParts = {
          year: "%Y", month: "%m", day: "%d", dateSep: ".",
          hour: "%H", minute: "%M", second: "%S", timezone: "%Z"
        };
        state.data.display.time_format = composeTimeFormat(state.timeFormatParts);
      }
      state.timeFormatCustom = false;
      render();
      return;
    }
    if (action === "tf-switch-custom") {
      event.preventDefault();
      state.timeFormatCustom = true;
      state.timeFormatParts = null;
      render();
      return;
    }
  });
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const payload = await res.json();
    state.data = payload.data;
    state.errors = payload.errors || [];
    state.status = payload.status || "";
    state.locked = Boolean(payload.locked);
    state.lockMessage = payload.lock_message || "";
    const parsedTf = parseTimeFormat((payload.data && payload.data.display && payload.data.display.time_format) || "");
    if (parsedTf) {
      state.timeFormatParts = parsedTf;
      state.timeFormatCustom = false;
    } else {
      state.timeFormatParts = null;
      state.timeFormatCustom = true;
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.errors = [`加载配置失败：${message}`];
    state.status = "";
    state.locked = true;
    state.lockMessage = "面板加载配置失败，请刷新页面或重启面板。";
  }
  render();
  updateRunnerUI();
}

async function saveConfig() {
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.data)
    });
    const payload = await res.json();
    state.errors = payload.errors || [];
    state.status = payload.status || "";
    if (payload.data) {
      state.data = payload.data;
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    state.errors = [`保存配置失败：${message}`];
    state.status = "";
  }
  render();
  updateRunnerUI();
}

bindEvents();
render();
loadConfig();
startRunnerPolling();
"""


_RUN_LOG_TAIL_BYTES = 12000


class _RunnerManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.runtime_dir = config_path.parent / "data" / "gui"
        self.run_pid_path = self.runtime_dir / "run.pid"
        self.run_log_path = self.runtime_dir / "run.log"
        self.once_log_path = self.runtime_dir / "once.log"
        self.lock = threading.Lock()

    def status_payload(self) -> dict[str, Any]:
        self._ensure_runtime_dir()
        running, pid = self._current_run()
        run_log = self._tail(self.run_log_path) if running else ""
        once_log = self._tail(self.once_log_path) if self.once_log_path.exists() else ""
        config_ok, session_ready, retention_days, requires_retention_confirm, message = (
            self._config_health()
        )
        return {
            "running": running,
            "pid": pid,
            "run_log": run_log,
            "once_log": once_log,
            "status": message or "",
            "config_ok": config_ok,
            "session_ready": session_ready,
            "retention_days": retention_days,
            "requires_retention_confirm": requires_retention_confirm,
        }

    def start_run(self, *, confirm_retention: bool = False) -> dict[str, Any]:
        with self.lock:
            running, pid = self._current_run()
            if running:
                return {"ok": True, "status": f"Run already active (pid {pid})."}
            config, message = self._load_config()
            if message:
                return {"ok": False, "status": message}
            session_ok, session_msg = self._session_ready(config)
            if not session_ok:
                return {"ok": False, "status": session_msg}
            if self._retention_confirm_required(config) and not confirm_retention:
                return {
                    "ok": False,
                    "status": (
                        "Retention confirmation required. "
                        "Please confirm retention risk in GUI before starting run daemon."
                    ),
                }
            self._ensure_runtime_dir()
            self._write_log_header(self.run_log_path, "Starting run daemon.")
            proc = self._spawn_process(
                [
                    "-m",
                    "tgwatch",
                    "run",
                    "--config",
                    str(self.config_path),
                    "--yes-retention",
                ],
                log_path=self.run_log_path,
            )
            self.run_pid_path.write_text(str(proc.pid), encoding="utf-8")
            return {"ok": True, "status": f"Run started (pid {proc.pid})."}

    def stop_run(self) -> dict[str, Any]:
        with self.lock:
            running, pid = self._current_run()
            if not running or pid is None:
                return {"ok": True, "status": "Run daemon is not active."}
            if not self._terminate_run_process(pid):
                return {"ok": False, "status": f"Failed to stop run daemon (pid {pid})."}
            self.run_pid_path.unlink(missing_ok=True)
            self._write_log_header(self.run_log_path, f"Stopped run daemon (pid {pid}).")
            return {"ok": True, "status": f"Run stopped (pid {pid})."}

    def start_once(
        self,
        since: str,
        target: str | None = None,
        push: bool = False,
    ) -> dict[str, Any]:
        if not since:
            return {"ok": False, "status": "since is required"}
        config, message = self._load_config()
        if message:
            return {"ok": False, "status": message}
        session_ok, session_msg = self._session_ready(config)
        if not session_ok:
            return {"ok": False, "status": session_msg}
        if target:
            target_key = target.strip()
            if target_key not in config.target_by_name:
                try:
                    target_chat_id = int(target_key)
                except (TypeError, ValueError):
                    return {"ok": False, "status": f"Unknown target: {target}"}
                if target_chat_id not in config.target_by_chat_id:
                    return {"ok": False, "status": f"Unknown target: {target}"}
        self._ensure_runtime_dir()
        header = f"Starting once (since {since})"
        args = ["-m", "tgwatch", "once", "--config", str(self.config_path), "--since", since]
        if target:
            args.extend(["--target", target])
            header += f" target={target}"
        if push:
            args.append("--push")
            header += " push=true"
        self._write_log_header(self.once_log_path, f"{header}.")
        self._spawn_process(
            args,
            log_path=self.once_log_path,
        )
        status = f"Once started (since {since})."
        if target:
            status = f"Once started (since {since}, target {target})."
        if push:
            status = status.replace(").", ", push enabled).")
        return {"ok": True, "status": status}

    def _ensure_runtime_dir(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def _current_run(self) -> tuple[bool, int | None]:
        pid = self._read_pid()
        if pid is None:
            return False, None
        if self._pid_is_running(pid) and self._pid_matches_run_daemon(pid):
            return True, pid
        if self._pid_is_running(pid):
            logger.warning("Ignoring PID %s from run.pid because it does not match tgwatch run daemon.", pid)
        self.run_pid_path.unlink(missing_ok=True)
        return False, None

    def _read_pid(self) -> int | None:
        if not self.run_pid_path.exists():
            return None
        try:
            value = int(self.run_pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
        return value if value > 0 else None

    def _pid_is_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return self._pid_exists_windows(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _pid_exists_windows(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return str(pid) in result.stdout

    def _pid_matches_run_daemon(self, pid: int) -> bool:
        """Best-effort identity check to avoid killing unrelated reused PIDs."""
        if os.name == "nt":
            # Windows tasklist output does not reliably include command args in this flow.
            return True
        command = self._pid_command(pid)
        if not command:
            return False
        argv = self._split_command(command)
        if not argv:
            return False
        has_config = self._command_uses_config(argv)
        has_run = self._command_is_tgwatch_run(argv)
        return has_run and has_config

    def _split_command(self, command: str) -> list[str]:
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()

    def _command_is_tgwatch_run(self, argv: list[str]) -> bool:
        if not argv:
            return False
        for idx, token in enumerate(argv):
            if token == "-m" and idx + 2 < len(argv):
                if argv[idx + 1] == "tgwatch" and argv[idx + 2] == "run":
                    return True
            if Path(token).name == "tgwatch" and idx + 1 < len(argv):
                if argv[idx + 1] == "run":
                    return True
        return False

    def _command_uses_config(self, argv: list[str]) -> bool:
        expected = self.config_path.resolve()
        for idx, token in enumerate(argv):
            value: str | None = None
            if token == "--config" and idx + 1 < len(argv):
                value = argv[idx + 1]
            elif token.startswith("--config="):
                value = token.partition("=")[2]
            if value is None:
                continue
            normalized = self._normalize_config_arg(value)
            if normalized is not None and normalized == expected:
                return True
        return False

    def _normalize_config_arg(self, value: str) -> Path | None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        return path.resolve()

    def _pid_command(self, pid: int) -> str:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _terminate_run_process(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                return False
            return result.returncode == 0 or not self._pid_is_running(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return not self._pid_is_running(pid)
        for _ in range(10):
            if not self._pid_is_running(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return not self._pid_is_running(pid)
        for _ in range(10):
            if not self._pid_is_running(pid):
                return True
            time.sleep(0.1)
        return not self._pid_is_running(pid)

    def _load_config(self) -> tuple[Any | None, str | None]:
        if not self.config_path.exists():
            return None, f"Config not found: {self.config_path.name}"
        try:
            return load_config(self.config_path), None
        except ConfigError as exc:
            return None, str(exc)

    def _session_ready(self, config: Any) -> tuple[bool, str | None]:
        if not config.telegram.session_file.exists():
            return False, "Session file not found. Run `python -m tgwatch run --config ...` once in a terminal."
        return True, None

    def _retention_confirm_required(self, config: Any) -> bool:
        retention_days = getattr(config.reporting, "retention_days", 30)
        return int(retention_days) > 180

    def _config_health(self) -> tuple[bool, bool, int, bool, str | None]:
        config, message = self._load_config()
        if message:
            return False, False, 30, False, message
        retention_days = int(getattr(config.reporting, "retention_days", 30))
        requires_retention_confirm = self._retention_confirm_required(config)
        session_ok, session_msg = self._session_ready(config)
        if not session_ok:
            return True, False, retention_days, requires_retention_confirm, session_msg
        return True, True, retention_days, requires_retention_confirm, None

    def _spawn_process(self, args: list[str], *, log_path: Path) -> subprocess.Popen:
        self._ensure_runtime_dir()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        cmd = [sys.executable, *args]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        try:
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                return subprocess.Popen(
                    cmd,
                    cwd=str(self.config_path.parent),
                    stdout=log_handle,
                    stderr=log_handle,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    creationflags=flags,
                )
            return subprocess.Popen(
                cmd,
                cwd=str(self.config_path.parent),
                stdout=log_handle,
                stderr=log_handle,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()

    def _write_log_header(self, path: Path, message: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {message} ===\n")

    def _tail(self, path: Path) -> str:
        if not path.exists():
            return ""
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > _RUN_LOG_TAIL_BYTES:
                    handle.seek(-_RUN_LOG_TAIL_BYTES, os.SEEK_END)
                data = handle.read()
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")


def run_gui(config_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local GUI server."""
    config_path = config_path.expanduser().resolve()
    server = _GuiServer((host, port), _GuiHandler, config_path=config_path)
    url = f"http://{host}:{port}"
    logger.info("GUI running at %s", url)
    print(f"GUI running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Failed to open browser: %s", exc)
    server.serve_forever()


class _GuiServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, *, config_path: Path):
        super().__init__(server_address, handler_cls)
        self.config_path = config_path
        self.runner = _RunnerManager(config_path)


class _GuiHandler(BaseHTTPRequestHandler):
    server_version = "tgwatch-gui/1.0"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_response(HTTPStatus.OK, _HTML, "text/html; charset=utf-8")
            return
        if path == "/app.css":
            self._send_response(HTTPStatus.OK, _CSS, "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_response(HTTPStatus.OK, _JS, "text/javascript; charset=utf-8")
            return
        if path == "/api/config":
            self._handle_get_config()
            return
        if path == "/api/runner/status":
            self._handle_runner_status()
            return
        if path == "/api/gui/stop":
            self._handle_gui_stop()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/config":
            self._handle_post_config()
            return
        if path == "/api/runner/run":
            self._handle_runner_run()
            return
        if path == "/api/runner/once":
            self._handle_runner_once()
            return
        if path == "/api/runner/stop":
            self._handle_runner_stop()
            return
        if path == "/api/gui/stop":
            self._handle_gui_stop()
            return
        if path == "/api/config/migrate":
            self._handle_migrate_config()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.info("GUI %s - %s", self.address_string(), format % args)

    def _handle_get_config(self) -> None:
        errors: list[str] = []
        locked = False
        lock_message = ""
        try:
            raw = _load_raw_config(self.server.config_path)
        except ConfigError as exc:
            raw = {}
            message = str(exc)
            errors.append(message)
            locked = True
            lock_message = message
        data = _normalize_config(raw)
        if self.server.config_path.exists():
            if not lock_message:
                try:
                    load_config(self.server.config_path)
                except ConfigError as exc:
                    message = str(exc)
                    errors.append(message)
                    locked = True
                    lock_message = message
        else:
            locked = True
            lock_message = (
                "config.toml not found. Copy config.example.toml and fill it, "
                "then reload the GUI."
            )
        payload = {
            "data": data,
            "errors": errors,
            "status": "",
            "locked": locked,
            "lock_message": lock_message,
        }
        self._send_json(HTTPStatus.OK, payload)

    def _handle_post_config(self) -> None:
        try:
            payload = self._read_json()
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"errors": ["Invalid JSON"]})
            return
        try:
            raw_existing = _load_raw_config(self.server.config_path)
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"errors": [str(exc)]})
            return
        errors, normalized = _validate_payload(payload, raw_existing)
        if errors:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"errors": errors, "data": _normalize_config(raw_existing)},
            )
            return
        toml_text = _render_toml(normalized, raw_existing)
        tmp_path = self.server.config_path.with_suffix(".tmp")
        tmp_path.write_text(toml_text, encoding="utf-8")
        try:
            load_config(tmp_path)
        except ConfigError as exc:
            tmp_path.unlink(missing_ok=True)
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"errors": [str(exc)], "data": _normalize_config(raw_existing)},
            )
            return
        tmp_path.replace(self.server.config_path)
        data = _normalize_config(_load_raw_config(self.server.config_path))
        self._send_json(HTTPStatus.OK, {"errors": [], "status": "Saved.", "data": data})

    def _handle_migrate_config(self) -> None:
        result = migrate_config(self.server.config_path)
        if not result.ok:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": result.status})
            return
        status = f"Migrated. Backup: {result.backup_path.name}. Review config.toml before running."
        self._send_json(HTTPStatus.OK, {"status": status})

    def _handle_runner_status(self) -> None:
        payload = self.server.runner.status_payload()
        self._send_json(HTTPStatus.OK, payload)

    def _handle_runner_run(self) -> None:
        try:
            request_payload = self._read_json()
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "Invalid JSON"})
            return
        confirm_retention = bool(request_payload.get("confirm_retention", False))
        payload = self.server.runner.start_run(confirm_retention=confirm_retention)
        status = HTTPStatus.OK if payload.get("ok", True) else HTTPStatus.BAD_REQUEST
        self._send_json(status, payload)

    def _handle_runner_once(self) -> None:
        try:
            payload = self._read_json()
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "Invalid JSON"})
            return
        since = str(payload.get("since", "")).strip()
        target = str(payload.get("target", "")).strip() or None
        push = bool(payload.get("push", False))
        response = self.server.runner.start_once(since, target, push)
        status = HTTPStatus.OK if response.get("ok", True) else HTTPStatus.BAD_REQUEST
        self._send_json(status, response)

    def _handle_runner_stop(self) -> None:
        payload = self.server.runner.stop_run()
        status = HTTPStatus.OK if payload.get("ok", True) else HTTPStatus.BAD_REQUEST
        self._send_json(status, payload)

    def _handle_gui_stop(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {"status": "GUI stopped. The run daemon (if running) stays active."},
        )
        shutdown_thread = threading.Thread(target=self.server.shutdown, daemon=True)
        shutdown_thread.start()

    def _send_response(self, status: HTTPStatus, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))


def _load_raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path.name}: {exc}") from exc


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    telegram = raw.get("telegram", {})
    sender_raw = raw.get("sender", {}) if isinstance(raw.get("sender"), dict) else {}
    reporting = raw.get("reporting", {})
    storage = raw.get("storage", {})
    display = raw.get("display", {})
    notifications = raw.get("notifications", {})

    api_hash = telegram.get("api_hash")
    data = {
        "config_version": raw.get("config_version", ""),
        "limits": {
            "maxTargets": MAX_TARGET_GROUPS,
            "maxUsersPerTarget": MAX_USERS_PER_TARGET,
            "maxControlGroups": MAX_CONTROL_GROUPS,
        },
        "telegram": {
            "api_id": telegram.get("api_id", ""),
            "api_hash": KEEP_SECRET if api_hash else "",
            "session_file": telegram.get("session_file", "data/tgwatch.session"),
        },
        "sender": {
            "enabled": bool(sender_raw),
            "session_file": sender_raw.get("session_file", "data/tgwatch_sender.session"),
        },
        "storage": {
            "db_path": storage.get("db_path", "data/tgwatch.sqlite3"),
            "media_dir": storage.get("media_dir", "data/media"),
        },
        "reporting": {
            "reports_dir": reporting.get("reports_dir", "reports"),
            "summary_interval_minutes": reporting.get("summary_interval_minutes", 120),
            "timezone": reporting.get("timezone", "UTC"),
            "retention_days": reporting.get("retention_days", 30),
        },
        "reporting_timezone_presets": _build_timezone_presets(),
        "display": {
            "show_ids": display.get("show_ids", True),
            "time_format": display.get("time_format", "%Y.%m.%d %H:%M:%S (%Z)"),
        },
        "display_time_format_units": _TIME_FORMAT_UNITS,
        "notifications": {
            "bark_key": notifications.get("bark_key", ""),
        },
    }

    targets_raw = []
    if "targets" in raw:
        targets_raw = raw.get("targets", []) or []
    elif "target" in raw:
        targets_raw = [raw.get("target", {})]
    targets: list[dict[str, Any]] = []
    for idx, target in enumerate(targets_raw, start=1):
        if not isinstance(target, dict):
            continue
        aliases = target.get("tracked_user_aliases", {}) or {}
        tracked_users = []
        for user_id in target.get("tracked_user_ids", []) or []:
            alias = aliases.get(user_id) or aliases.get(str(user_id)) or ""
            tracked_users.append({"id": str(user_id), "alias": alias})
        if not tracked_users:
            tracked_users = [{"id": "", "alias": ""}]
        targets.append(
            {
                "name": target.get("name") or f"group-{idx}",
                "target_chat_id": target.get("target_chat_id", ""),
                "summary_interval_minutes": target.get("summary_interval_minutes", ""),
                "control_group": target.get("control_group", ""),
                "tracked_users": tracked_users,
            }
        )
    if not targets:
        targets = [blank_target()]
    data["targets"] = targets

    control_groups_raw: dict[str, Any] = {}
    if "control_groups" in raw:
        control_groups_raw = raw.get("control_groups", {}) or {}
    elif "control" in raw:
        control_groups_raw = {"default": raw.get("control", {})}
    control_groups: list[dict[str, Any]] = []
    for key, group in control_groups_raw.items():
        if not isinstance(group, dict):
            continue
        topic_map = []
        topic_target_raw = group.get("topic_target_map", {}) or {}
        if isinstance(topic_target_raw, dict):
            for target_id, user_map in topic_target_raw.items():
                if not isinstance(user_map, dict):
                    continue
                for user_id, topic_id in user_map.items():
                    target_text = str(target_id)
                    user_text = str(user_id)
                    topic_map.append(
                        {
                            "user_key": f"{target_text}|{user_text}",
                            "target_chat_id": target_text,
                            "user_id": user_text,
                            "topic_id": str(topic_id),
                        }
                    )
        if not topic_map:
            topic_map = [{"user_key": "", "target_chat_id": "", "user_id": "", "topic_id": ""}]
        control_groups.append(
            {
                "key": str(key),
                "control_chat_id": group.get("control_chat_id", ""),
                "is_forum": bool(group.get("is_forum", False)),
                "topic_routing_enabled": bool(group.get("topic_routing_enabled", False)),
                "skip_html_report": bool(group.get("skip_html_report", False)),
                "topic_target_map": topic_map,
            }
        )
    if not control_groups:
        control_groups = [blank_control_group()]
    data["control_groups"] = control_groups
    return data


def blank_target() -> dict[str, Any]:
    return {
        "name": "",
        "target_chat_id": "",
        "summary_interval_minutes": "",
        "control_group": "",
        "tracked_users": [{"id": "", "alias": ""}],
    }


def blank_control_group() -> dict[str, Any]:
    return {
        "key": "",
        "control_chat_id": "",
        "is_forum": False,
        "topic_routing_enabled": False,
        "skip_html_report": False,
        "topic_target_map": [
            {"user_key": "", "target_chat_id": "", "user_id": "", "topic_id": ""}
        ],
    }


def _validate_payload(payload: dict[str, Any], raw_existing: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []

    telegram = payload.get("telegram", {}) or {}
    api_id_raw = str(telegram.get("api_id", "")).strip()
    api_hash_raw = str(telegram.get("api_hash", "")).strip()
    session_file = str(telegram.get("session_file", "data/tgwatch.session")).strip()

    if not api_id_raw:
        errors.append("telegram.api_id is required")
    if api_hash_raw in {"", KEEP_SECRET}:
        api_hash_raw = str(raw_existing.get("telegram", {}).get("api_hash", "")).strip()
    if not api_hash_raw:
        errors.append("telegram.api_hash is required")

    sender = payload.get("sender", {}) or {}
    sender_enabled = bool(sender.get("enabled", False))
    sender_session = str(sender.get("session_file", "")).strip()
    if sender_enabled and not sender_session:
        errors.append("sender.session_file is required when sender is enabled")
    if sender_enabled and sender_session and sender_session == session_file:
        errors.append("sender.session_file must differ from telegram.session_file")

    targets_raw = payload.get("targets", []) or []
    control_raw = payload.get("control_groups", []) or []

    if not targets_raw:
        errors.append("At least one target group is required")
    if len(targets_raw) > MAX_TARGET_GROUPS:
        errors.append(f"Targets cannot exceed {MAX_TARGET_GROUPS}")
    if not control_raw:
        errors.append("At least one control group is required")
    if len(control_raw) > MAX_CONTROL_GROUPS:
        errors.append(f"Control groups cannot exceed {MAX_CONTROL_GROUPS}")

    control_groups: list[dict[str, Any]] = []
    control_keys: list[str] = []
    for idx, raw in enumerate(control_raw, start=1):
        key = str(raw.get("key", "")).strip()
        if not key:
            errors.append(f"Control group #{idx} requires a key")
        if key in control_keys:
            errors.append(f"Duplicate control group key: {key}")
        control_keys.append(key)
        chat_id = _coerce_int(raw.get("control_chat_id"), f"control_groups[{key}].control_chat_id", errors)
        is_forum = bool(raw.get("is_forum", False))
        skip_html_report = bool(raw.get("skip_html_report", False))
        topic_enabled = bool(raw.get("topic_routing_enabled", False))
        topic_map_entries = raw.get("topic_target_map", []) or []
        topic_map = []
        if topic_enabled:
            for entry in topic_map_entries:
                user_key = str(entry.get("user_key", "")).strip()
                target_chat_id = None
                user_id = None
                if user_key:
                    parts = user_key.split("|", 1)
                    if len(parts) == 2:
                        target_chat_id = _coerce_int(
                            parts[0], f"control_groups[{key}].topic_target_map.target_chat_id", errors
                        )
                        user_id = _coerce_int(
                            parts[1], f"control_groups[{key}].topic_target_map.user_id", errors
                        )
                    else:
                        errors.append(f"control_groups[{key}] topic map user selection is invalid")
                else:
                    target_chat_id = _coerce_int(
                        entry.get("target_chat_id"),
                        f"control_groups[{key}].topic_target_map.target_chat_id",
                        errors,
                    )
                    user_id = _coerce_int(
                        entry.get("user_id"),
                        f"control_groups[{key}].topic_target_map.user_id",
                        errors,
                    )
                topic_id = _coerce_int(
                    entry.get("topic_id"), f"control_groups[{key}].topic_target_map.topic_id", errors
                )
                if target_chat_id is not None and user_id is not None and topic_id is not None:
                    topic_map.append(
                        {"target_chat_id": target_chat_id, "user_id": user_id, "topic_id": topic_id}
                    )
        else:
            # Keep existing mappings without strict validation while routing is disabled.
            for entry in topic_map_entries:
                user_key = str(entry.get("user_key", "")).strip()
                target_chat_id = None
                user_id = None
                if user_key:
                    parts = user_key.split("|", 1)
                    if len(parts) == 2:
                        target_chat_id = _try_int(parts[0])
                        user_id = _try_int(parts[1])
                else:
                    target_chat_id = _try_int(entry.get("target_chat_id"))
                    user_id = _try_int(entry.get("user_id"))
                topic_id = _try_int(entry.get("topic_id"))
                if target_chat_id is not None and user_id is not None and topic_id is not None:
                    topic_map.append(
                        {"target_chat_id": target_chat_id, "user_id": user_id, "topic_id": topic_id}
                    )
        if topic_enabled and not is_forum:
            errors.append(f"control_groups[{key}] topic routing requires forum mode")
        if topic_enabled and not topic_map:
            errors.append(f"control_groups[{key}] topic routing requires at least one mapping")
        control_groups.append(
            {
                "key": key,
                "control_chat_id": chat_id,
                "is_forum": is_forum,
                "topic_routing_enabled": topic_enabled,
                "skip_html_report": skip_html_report,
                "topic_target_map": topic_map,
            }
        )

    default_control = control_keys[0] if len(control_keys) == 1 else None

    targets: list[dict[str, Any]] = []
    mapped_users: dict[str, dict[int, set[int]]] = {key: {} for key in control_keys}
    for idx, raw in enumerate(targets_raw, start=1):
        name = str(raw.get("name", "")).strip()
        if not name:
            name = f"group-{idx}"
        chat_id = _coerce_int(raw.get("target_chat_id"), f"targets[{idx}].target_chat_id", errors)
        interval_raw = str(raw.get("summary_interval_minutes", "")).strip()
        interval = None
        if interval_raw:
            interval = _coerce_int(interval_raw, f"targets[{idx}].summary_interval_minutes", errors)
            if interval is not None and interval <= 0:
                errors.append(f"targets[{idx}].summary_interval_minutes must be > 0")
        control_group = str(raw.get("control_group", "")).strip() or default_control
        if not control_group:
            errors.append(f"targets[{idx}] must map to a control_group")
        if control_group and control_group not in control_keys:
            errors.append(f"targets[{idx}] references unknown control_group '{control_group}'")
        tracked_users_raw = raw.get("tracked_users", []) or []
        if not tracked_users_raw:
            errors.append(f"targets[{idx}].tracked_users cannot be empty")
        if len(tracked_users_raw) > MAX_USERS_PER_TARGET:
            errors.append(f"targets[{idx}] cannot exceed {MAX_USERS_PER_TARGET} users")
        tracked_ids: list[int] = []
        aliases: dict[int, str] = {}
        for uidx, entry in enumerate(tracked_users_raw, start=1):
            user_id = _coerce_int(entry.get("id"), f"targets[{idx}].tracked_users[{uidx}].id", errors)
            if user_id is None:
                continue
            if user_id in tracked_ids:
                errors.append(f"targets[{idx}] has duplicate user_id {user_id}")
                continue
            tracked_ids.append(user_id)
            alias = str(entry.get("alias", "")).strip()
            if alias:
                aliases[user_id] = alias
            if control_group and chat_id is not None:
                mapped_users.setdefault(control_group, {}).setdefault(chat_id, set()).add(user_id)
        targets.append(
            {
                "name": name,
                "target_chat_id": chat_id,
                "tracked_user_ids": tracked_ids,
                "tracked_user_aliases": aliases,
                "summary_interval_minutes": interval,
                "control_group": control_group,
            }
        )

    seen_topics: set[tuple[str, int, int]] = set()
    for group in control_groups:
        if not group["topic_routing_enabled"]:
            continue
        allowed_by_target = mapped_users.get(group["key"], {})
        for entry in group["topic_target_map"]:
            target_chat_id = entry["target_chat_id"]
            user_id = entry["user_id"]
            key = (group["key"], target_chat_id, user_id)
            if key in seen_topics:
                errors.append(
                    f"control_groups[{group['key']}] has duplicate topic mapping for "
                    f"{target_chat_id}:{user_id}"
                )
            seen_topics.add(key)
            allowed = allowed_by_target.get(target_chat_id, set())
            if not allowed:
                errors.append(
                    f"control_groups[{group['key']}] topic map references unknown target {target_chat_id}"
                )
                continue
            if user_id not in allowed:
                errors.append(
                    f"control_groups[{group['key']}] topic map includes unknown user {user_id} "
                    f"for target {target_chat_id}"
                )

    reporting = payload.get("reporting", {}) or {}
    storage = payload.get("storage", {}) or {}
    display = payload.get("display", {}) or {}
    notifications = payload.get("notifications", {}) or {}

    normalized = {
        "config_version": 1.0,
        "telegram": {
            "api_id": _coerce_int(api_id_raw, "telegram.api_id", errors) or 0,
            "api_hash": api_hash_raw,
            "session_file": session_file or "data/tgwatch.session",
        },
        "sender": {
            "enabled": sender_enabled,
            "session_file": sender_session,
        },
        "targets": targets,
        "control_groups": control_groups,
        "storage": {
            "db_path": str(storage.get("db_path", "data/tgwatch.sqlite3")).strip(),
            "media_dir": str(storage.get("media_dir", "data/media")).strip(),
        },
        "reporting": {
            "reports_dir": str(reporting.get("reports_dir", "reports")).strip(),
            "summary_interval_minutes": _coerce_int(
                reporting.get("summary_interval_minutes", 120),
                "reporting.summary_interval_minutes",
                errors,
            )
            or 120,
            "timezone": str(reporting.get("timezone", "UTC")).strip() or "UTC",
            "retention_days": _coerce_int(
                reporting.get("retention_days", 30),
                "reporting.retention_days",
                errors,
            )
            or 30,
        },
        "display": {
            "show_ids": bool(display.get("show_ids", True)),
            "time_format": str(display.get("time_format", "%Y.%m.%d %H:%M:%S (%Z)")).strip()
            or "%Y.%m.%d %H:%M:%S (%Z)",
        },
        "notifications": {
            "bark_key": str(notifications.get("bark_key", "")).strip(),
        },
    }

    return errors, normalized


def _coerce_int(value: Any, label: str, errors: list[str]) -> int | None:
    if value is None:
        errors.append(f"{label} is required")
        return None
    if isinstance(value, str):
        value = value.strip()
    if value == "":
        errors.append(f"{label} is required")
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be an integer")
        return None


def _try_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
    if value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_toml(config: dict[str, Any], raw_existing: dict[str, Any]) -> str:
    lines: list[str] = []
    config_version = config.get("config_version", 1.0)
    lines.append(f"config_version = {config_version}")
    telegram = config["telegram"]
    api_hash = telegram["api_hash"]
    if api_hash in {"", KEEP_SECRET}:
        api_hash = str(raw_existing.get("telegram", {}).get("api_hash", ""))
    lines.extend(
        [
            "[telegram]",
            f"api_id = {telegram['api_id']}",
            f"api_hash = {toml_string(api_hash)}",
            f"session_file = {toml_string(telegram['session_file'])}",
        ]
    )

    sender = config["sender"]
    if sender.get("enabled"):
        lines.extend(
            [
                "",
                "[sender]",
                f"session_file = {toml_string(sender.get('session_file', ''))}",
            ]
        )

    for target in config["targets"]:
        lines.extend(
            [
                "",
                "[[targets]]",
                f"name = {toml_string(target['name'])}",
                f"target_chat_id = {target['target_chat_id']}",
                f"tracked_user_ids = {toml_list(target['tracked_user_ids'])}",
            ]
        )
        if target.get("summary_interval_minutes"):
            lines.append(f"summary_interval_minutes = {target['summary_interval_minutes']}")
        if target.get("control_group"):
            lines.append(f"control_group = {toml_string(target['control_group'])}")
        aliases = target.get("tracked_user_aliases", {})
        if aliases:
            lines.append("")
            lines.append("[targets.tracked_user_aliases]")
            for user_id, alias in aliases.items():
                lines.append(f"{user_id} = {toml_string(alias)}")

    for group in config["control_groups"]:
        key = group["key"]
        quoted_key = toml_string(str(key))
        lines.extend(
            [
                "",
                f"[control_groups.{quoted_key}]",
                f"control_chat_id = {group['control_chat_id']}",
                f"is_forum = {toml_bool(group['is_forum'])}",
                f"topic_routing_enabled = {toml_bool(group['topic_routing_enabled'])}",
                f"skip_html_report = {toml_bool(group.get('skip_html_report', False))}",
            ]
        )
        topic_map = group.get("topic_target_map", [])
        if topic_map:
            by_target: dict[int, list[dict[str, Any]]] = {}
            for entry in topic_map:
                target_id = entry.get("target_chat_id")
                if target_id is None:
                    continue
                by_target.setdefault(int(target_id), []).append(entry)
            for target_id, entries in by_target.items():
                lines.append("")
                lines.append(
                    f"[control_groups.{quoted_key}.topic_target_map.{toml_string(str(target_id))}]"
                )
                for entry in entries:
                    lines.append(f"{entry['user_id']} = {entry['topic_id']}")

    storage = config["storage"]
    reporting = config["reporting"]
    display = config["display"]
    notifications = config["notifications"]

    lines.extend(
        [
            "",
            "[storage]",
            f"db_path = {toml_string(storage['db_path'])}",
            f"media_dir = {toml_string(storage['media_dir'])}",
            "",
            "[reporting]",
            f"reports_dir = {toml_string(reporting['reports_dir'])}",
            f"summary_interval_minutes = {reporting['summary_interval_minutes']}",
            f"timezone = {toml_string(reporting['timezone'])}",
            f"retention_days = {reporting['retention_days']}",
            "",
            "[display]",
            f"show_ids = {toml_bool(display['show_ids'])}",
            f"time_format = {toml_string(display['time_format'])}",
            "",
            "[notifications]",
            f"bark_key = {toml_string(notifications.get('bark_key', ''))}",
        ]
    )

    return "\n".join(lines).strip() + "\n"


def toml_string(value: str) -> str:
    value = value.replace("\\", "\\\\").replace('"', "\\\"")
    return f'"{value}"'


def toml_bool(value: bool) -> str:
    return "true" if value else "false"


def toml_list(values: list[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"
