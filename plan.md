# Plan — telegram-watch

> Generated: 2026-03-27 | Version: v1.6.0 | Branch: `dev`

## Project Status: Active Development

## Completed Milestones

| Milestone | Key REQs | Version |
|-----------|----------|---------|
| MVP bootstrap (capture, report, control chat) | REQ-001 ~ 006 | v0.1.x |
| Media, reply context, push notifications | REQ-007 ~ 014 | v0.2.x ~ v0.3.x |
| Bark notifications, heartbeat, error handling | REQ-018 ~ 024 | v0.3.x |
| Topic routing, multi-admin, bridge mode | REQ-025-002 ~ 129-002 | v0.4.x ~ v1.0.0 |
| GUI (config editor, launcher, stop/run controls) | REQ-203 ~ 206 | v1.1.x ~ v1.3.x |
| Forum reply disambiguation, CLI unification | REQ-212-001 ~ 004 | v1.0.4 |
| Skip HTML report, network reconnect resilience | REQ-304-001, REQ-310-001 | v1.5.0 |
| Realtime push mode + cloud-sync resilience + GUI i18n | REQ-20260320-001 | v1.6.0 |

## Active Backlog

| REQ | Title | Status | Target Version |
|-----|-------|--------|----------------|
| REQ-20260320-001 | 即时推送模式 (Realtime Push Mode) | **Done** | v1.6.0 |
| REQ-20260327-001 | 更新检查 / 心跳可配置 / 语言设置 | **Implementing** | v1.7.0 |

## Current Focus

### REQ-20260327-001: Update Check, Heartbeat Config, Language Setting

**概要**: 自动检查 GitHub Release 更新并推送通知到控制群；心跳间隔可配置（可关闭）；新增后端语言设置，所有推送消息跟随语言。

**三大功能模块**:

| 功能 | Config 字段 | 默认值 | 位置 |
|------|------------|--------|------|
| 自动更新检查 | `notifications.check_updates` | `true` | `[notifications]` |
| 心跳间隔 | `notifications.heartbeat_interval_minutes` | `120`（0=关闭） | `[notifications]` |
| 推送语言 | `display.language` | `"auto"` | `[display]` |

**实现计划**:

#### Phase 1（并行）

- [ ] **Worker A: config 层** — `config.py` + `config.example.toml`
  - `DisplayConfig` 加 `language: str`（`"auto"` / `"zh"` / `"en"`）
  - `NotificationConfig` 加 `heartbeat_interval_minutes: int`、`check_updates: bool`
  - `_parse_display()` / `_parse_notifications()` 更新
  - `config.example.toml` 加三个新字段

- [ ] **Worker B: update_checker 模块** — `telegram_watch/update_checker.py` + `tests/test_update_checker.py`
  - `check_for_update(current_version) -> UpdateInfo | None`：调用 GitHub API，10s timeout，失败静默
  - `should_notify(data_dir, version) -> bool`：读 `data/update_notified.json`，同版本 < 3 次才返回 True
  - `record_notification(data_dir, version)`：写入 JSON，递增 count
  - `format_notification(update_info, language) -> str`：根据语言格式化通知文本
  - 单元测试覆盖：版本比较、通知计数、格式化、API 失败静默

#### Phase 2（并行，依赖 Phase 1 完成）

- [ ] **Worker C: runner 集成** — `telegram_watch/runner.py`
  - `_HeartbeatLoop`：`_IDLE_SECONDS` 改读 `config.notifications.heartbeat_interval_minutes * 60`；为 0 时不启动；消息跟随语言
  - 新增 `_UpdateCheckLoop`：启动时检查 + 每 24h 检查；调用 update_checker；推送到所有 control group
  - `run_daemon()` 中初始化两个 loop

- [ ] **Worker D: GUI** — `telegram_watch/gui.py`
  - i18n 字典加新 key（heartbeat interval / check updates / language 相关，中英双语）
  - Display 区加语言选择器（auto / 中文 / English）
  - Notifications 区加 heartbeat interval 输入框 + check_updates 开关
  - 语言切换按钮点击时同步写入 `state.data.display.language`
  - 加载 config 时用 `data.display.language` 覆盖 localStorage 语言

#### Phase 3（顺序）

- [ ] **Worker E: 文档** — CHANGELOG (×4) + configuration docs (×4) + README 如有必要
  - CHANGELOG v1.7.0 条目（4 语言）
  - configuration.md 更新三个新字段说明（4 语言）
  - 版本号 → v1.7.0

**接口约定（Worker A ↔ B 并行时）**:
```python
# config.py 数据结构
class DisplayConfig:
    show_ids: bool
    time_format: str
    language: str  # "auto" | "zh" | "en"

class NotificationConfig:
    bark_key: str | None
    heartbeat_interval_minutes: int  # 0 = disabled, default 120
    check_updates: bool  # default True

# update_checker.py 对外接口
async def check_and_notify(
    config: Config,
    client: TelegramClient,
    fallback_client: TelegramClient | None,
    data_dir: Path,
) -> None
```

**风险点**:
1. GitHub API 超时 → 10s timeout + 静默失败
2. `data/update_notified.json` 损坏 → JSON parse 失败时重置为空
3. GUI 语言联动 → Save 时写 config，Load 时从 config 覆盖 localStorage

## What's Next

1. 完成 REQ-20260327-001 → 发布 v1.7.0
2. Potential follow-ups:
   - 条件触发（关键词过滤即时推送）
   - 消息聚合模式（N 秒内的消息合并为一条推送）
