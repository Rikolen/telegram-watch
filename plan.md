# Plan — telegram-watch

> Generated: 2026-03-20 | Version: v1.5.0 | Branch: `dev`

## Project Status: Stable / Maintenance

The core product (MTProto user-account watcher with GUI, bridge mode, topic routing, and multi-admin support) is feature-complete at v1.5.0. All 70+ requirements have been delivered and archived to `docs/requests/Done/`.

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

## Active Backlog

| REQ | Title | Status | Target Version |
|-----|-------|--------|----------------|
| REQ-20260320-001 | 即时推送模式 (Realtime Push Mode) | **Done** | v1.6.0 |

## Current Focus

### REQ-20260320-001: Realtime Push Mode
- **概要**: 新增即时模式，消息到达即刻推送至控制群组，HTML 报告按独立周期汇总
- **7 层速率防护**: 滑动窗口 / 间隔抖动 / 媒体延迟 / 长周期上限 / 指数退避 / 熔断器 / 启动冷却
- **实现进度**:
  - [x] REQ 审批通过，状态 → Implementing
  - [x] Worker 1: `telegram_watch/rate_limiter.py`（速率防护模块）✅
  - [x] Worker 2: `config.py` + `config.example.toml`（配置项）✅
  - [x] Worker 3: `runner.py`（RealtimePusher 集成 + 启动日志）✅
  - [x] Worker 4: 单元测试（51 new tests）✅
  - [x] Worker 5: 配置文档（4 语言）✅
  - [x] 收尾：CHANGELOG（4 语言）+ 版本 → v1.6.0 + 全量 144 tests pass ✅

## What's Next

1. 审批 REQ-20260320-001 → 实现 → 发布 v1.6.0
2. Potential follow-ups:
   - GUI 中增加模式切换开关
   - 条件触发（关键词过滤即时推送）
   - 消息聚合模式（N 秒内的消息合并为一条推送）
