# REQ-20260327-001-update-check-heartbeat-language

Status: Implementing
Owner: @yuxiao
Created: 2026-03-27

## Summary
Add automatic update checking with control-chat notifications, configurable heartbeat interval, and a language setting that governs all backend push messages.

## Release Impact
- 预计版本号：`1.6.0 -> 1.7.0`
- 预告 changelog 文案（英文）：`Add automatic update check with control-chat notifications, configurable heartbeat interval, and backend language setting for push messages.`

## Motivation
- Users have no way to know a new release is available unless they manually check GitHub.
- Heartbeat interval is hardcoded at 2 hours with no way to adjust or disable.
- Push messages (heartbeat, update notifications) are English-only, inconsistent with the GUI's i18n support.

## Scope (MVP)

必须做：
- [ ] **Update check**: on daemon startup + every 24 hours, query GitHub API for latest release
- [ ] **Update notification**: if new version detected, push to all control groups (same path as heartbeat)
- [ ] **Notification content**: use CHANGELOG entry for the new version (concise), follow user's language setting; include release link
- [ ] **Push frequency**: same version pushed up to 3 times (once per 24h check cycle), then stop; track notified version+count in a local file
- [ ] **Config: `language`** field (`"auto"` | `"zh"` | `"en"`, default `"auto"`); `"auto"` detects from system locale; GUI language toggle syncs to this field on save
- [ ] **Config: `heartbeat_interval_minutes`** (default `120`, set to `0` to disable)
- [ ] **Config: `check_updates`** (default `true`, set to `false` to disable)
- [ ] **GUI controls**: add heartbeat interval, check_updates toggle, and language selector to GUI (with i18n labels in zh/en)
- [ ] **Heartbeat i18n**: "Watcher is still running" → follows language setting (zh: "监控仍在运行中")
- [ ] **Update notification i18n**: notification text follows language setting

不做（明确排除项）：
- [ ] Auto-download or auto-install updates
- [ ] In-app update mechanism (user runs `git pull` themselves)
- [ ] GUI-side update banner (only push to control chat via Telegram)

## Functional Requirements
- [ ] **Trigger**: daemon startup + every 24 hours thereafter (naturally staggered across users by startup time)
- [ ] **GitHub API**: `GET https://api.github.com/repos/o1xhack/telegram-watch/releases/latest` (unauthenticated, 60 req/hr limit — 1 req/24h per user is negligible)
- [ ] **Version compare**: compare `tag_name` from API vs `pyproject.toml` version using semver
- [ ] **Notification destination**: all control groups, same as heartbeat path
- [ ] **Notification content (en)**:
  ```
  🆕 telegram-watch v1.7.0 available (current: v1.6.0)

  What's New:
  - <CHANGELOG entry for the new version>

  https://github.com/o1xhack/telegram-watch/releases/tag/v1.7.0
  ```
- [ ] **Notification content (zh)**:
  ```
  🆕 telegram-watch v1.7.0 已发布（当前版本：v1.6.0）

  更新内容：
  - <CHANGELOG 中对应版本的条目>

  https://github.com/o1xhack/telegram-watch/releases/tag/v1.7.0
  ```
- [ ] **Notified tracking**: store `{"version": "v1.7.0", "count": 2}` in `data/update_notified.json`; increment on each push; stop at 3
- [ ] **Heartbeat message (en)**: "Watcher is still running"
- [ ] **Heartbeat message (zh)**: "监控仍在运行中"
- [ ] **GUI language toggle**: switching language in GUI writes `language` field to config on next Save

## Non-Functional Requirements
- [ ] Mac 本地可运行
- [ ] 免费方案（GitHub public API, no token needed）
- [ ] 隐私/安全：only fetches public release metadata; no PII sent
- [ ] 性能：1 HTTP request per 24h; timeout 10s with silent failure on error
- [ ] Backward compatible: missing config keys use defaults (updates on, heartbeat 120min, language auto)

## Acceptance Criteria (DoD)
- [ ] `check_updates = true` (default): daemon pushes update notification to control chat when new release exists
- [ ] `check_updates = false`: no GitHub API calls, no notifications
- [ ] Same version notified at most 3 times across daemon restarts
- [ ] `heartbeat_interval_minutes = 120`: heartbeat fires after 2h idle (existing behavior)
- [ ] `heartbeat_interval_minutes = 0`: no heartbeat messages
- [ ] `heartbeat_interval_minutes = 60`: heartbeat fires after 1h idle
- [ ] `language = "zh"`: heartbeat and update notifications in Chinese
- [ ] `language = "en"`: heartbeat and update notifications in English
- [ ] `language = "auto"`: detect from system locale
- [ ] GUI shows heartbeat interval, check_updates toggle, language selector with i18n labels
- [ ] GUI language toggle updates config `language` field on Save
- [ ] Passes `pytest tests/`
- [ ] config.example.toml updated with new fields
- [ ] All 4 CHANGELOGs updated
- [ ] All 4 READMEs updated if Key Features change

## Implementation Notes (for Codex)
- 相关模块/文件：`runner.py` (_HeartbeatLoop, run_daemon), `config.py`, `gui.py`, `config.example.toml`
- 新增模块：`update_checker.py` (GitHub API call, version compare, notification logic)
- 风险点：GitHub API rate limit (mitigated by 1 req/24h); network failure (silent ignore)
- 需要更新的文档：config.example.toml, docs/configuration.md (4 languages), CHANGELOG (4 languages)
