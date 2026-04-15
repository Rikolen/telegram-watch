# 更新日志

[English](CHANGELOG.md) | [简体中文](CHANGELOG.zh-Hans.md) | [繁體中文](CHANGELOG.zh-Hant.md) | [日本語](CHANGELOG.ja.md)

> 条目按时间从新到旧排列，最新版本在最上方。每条变更都会标注对应的需求编号。

## 1.7.0 — 2026-04-14
- 新增全局消息模板切换，对**所有转发到控制群的单条消息**生效——无论 `interval` 模式（每次定时汇总后逐条转发）还是 `realtime` 模式（即时推送），均按所选模板渲染。在「显示与通知」中可在 `Normal`（标准）与 `Minimal`（极简）之间选择。极简模板把发送者与正文合并到第一行，时间下移到第二行；标准模板维持原有分行版面。GUI 侧新增实时预览面板，可直观对比两种样式。ID 显示、时间格式、语言等既有定制项均叠加生效；老配置缺少 `display.template` 键时自动回退为 `normal`，无需手动迁移。
- 重新梳理「显示与通知」设置区，拆分为「消息模板 / 消息字段 / 语言 / 通知」四个子分组，相关选项集中呈现。

## 1.6.1 — 2026-03-30
- 修复更新检查器报告过期版本号的问题（如显示 1.0.4 而非 1.6.0）。现优先从 pyproject.toml 读取版本，仅在冻结环境下回退到 importlib.metadata。

## 1.6.0 — 2026-03-27
- [实验性] 新增"即时推送模式"：被追踪用户的消息到达后立即转发至控制群组，HTML 报告按独立周期汇总生成。内置 7 层速率防护体系（滑动窗口限流、随机抖动间隔、媒体额外延迟、每小时/每日上限、指数退避、熔断器 + Bark 告警、启动冷却期），防止 Telegram 账号受到限制（REQ-20260320-001-realtime-push-mode）。
- 为所有 SQLite 数据库（应用数据库和 Telethon session）启用 WAL 模式和 busy_timeout，提升云同步目录下的稳定性。新增 I/O 错误自动重试机制，doctor 命令和 GUI 检测到数据文件位于云同步目录时输出警告（REQ-20260321-001-sqlite-wal-retry）。
- GUI 新增国际化（i18n）支持：支持自动检测和手动切换中文/英文界面。
- 新增自动更新检查：daemon 启动时及每 24 小时查询 GitHub Releases，新版本最多向所有控制群推送 3 次通知并附 Release 链接（REQ-20260327-001-update-check-heartbeat-language）。
- 心跳间隔可通过 `notifications.heartbeat_interval_hours` 配置（默认 2 小时，设为 0 关闭）。心跳消息跟随语言设置（REQ-20260327-001-update-check-heartbeat-language）。
- 新增 `display.language` 设置（`"auto"` / `"zh"` / `"en"`），控制所有后端推送消息的语言（REQ-20260327-001-update-check-heartbeat-language）。
- 修复 GUI 状态轮询时重复打印实验模式警告的问题。

## 1.5.0 — 2026-03-11
- 新增控制群级别 `skip_html_report` 选项，开启后推送到控制群时仅发送逐条消息，不发送 HTML 报告文件（REQ-20260310-001-skip-html-report-option）。
- 新增 GitHub Actions 工作流，支持定时每日消息抓取与 Artifact 报告存储，并为 CI 环境增加非交互模式支持（REQ-20260310-001-github-actions-daily-summary）。
- 新增 daemon 模式网络断线自动重连功能，采用指数退避策略（10s→300s）。监控进程在临时网络故障时不再崩溃退出，重连成功后向控制群发送恢复通知（REQ-20260304-001-daemon-reconnect-on-network-loss）。

## 1.0.0 — 2026-02-04
- 交付多目标监控与控制群路由，并提供本地 GUI 与控制群映射体验优化（REQ-20260202-001-multi-admin-monitoring，REQ-20260203-001-config-gui-design，REQ-20260204-003-gui-control-mapping-ux）。
- 新增一键启动脚本与 GUI 运行控制（run/once、后台日志、Stop GUI），并修复 GUI 启动崩溃（REQ-20260203-002-gui-launcher-and-runner，REQ-20260204-001-gui-launcher-loglevel-fix，REQ-20260204-002-gui-stop-button）。
- 强制 config_version = 1.0，按 target_chat_id + user_id 的 Topic 映射，并加入应用内迁移流程（REQ-20260204-004-topic-mapping-per-target，REQ-20260204-006-config-migration-flow）。
- 补齐配置迁移与默认命名相关测试，并刷新文档说明（REQ-20260205-001-audit-tests-docs）。
- 简化迁移流程，只保留 `config-old-0.1.toml` 备份（REQ-20260205-002-drop-config-sample）。
- 新增 run once 单目标过滤（CLI/GUI 可选单一群组）（REQ-20260205-003-once-target-filter）。
- 将 `config-old-*.toml` 迁移备份加入 git 忽略（REQ-20260205-004-ignore-old-configs）。
- GUI 增加 run once 推送开关与日志显示上限（REQ-20260205-005-gui-once-push-toggle）。
- GUI 新增启动前保护：缺少 session 时醒目提示并禁用 Run/Once，`retention_days > 180` 改为界面确认，避免终端 y/n 卡住（REQ-20260205-006-gui-run-guards）。
- 优化 GUI retention 交互：Run daemon 保持可点击，点击后进入确认流程（勾选后确认按钮才可用）再启动长保留运行（REQ-20260205-007-gui-retention-click-confirm-flow）。
- GUI 新增 `Stop daemon` 控制，并修复 run 启动后 retention 确认框不消失的问题，可直接在 Runner 面板管理 daemon 生命周期（REQ-20260205-008-gui-run-stop-and-confirm-dismiss）。
- 在 push 前补强 GUI Runner 错误处理路径，并同步 run/stop/retention 确认流程文档（REQ-20260205-009-pre-push-calibration-audit）。
- 启动脚本改为 Conda（`tgwatch`）优先并自动回退 venv，同时同步多语言安装文档（REQ-20260205-010-launcher-conda-prefer-fallback-venv）。
- 增强启动器稳健性：macOS 启动器兼容 bash，且安装引导在 pip 工具升级失败时会给出明确警告并继续尝试（REQ-20260205-011-launcher-shell-and-bootstrap-robustness）。

## 0.3.0 — 2026-01-29
- 新增双账号桥接：由发送端账号推送控制群消息，使主账号恢复通知（REQ-20260129-002-bridge-implementation）。
- 双账号登录时补充主账号/发送账号提示，避免混淆（REQ-20260129-003-sender-login-prompt）。
- 双账号登录提示改为用户友好文本，并在终端明确区分主账号/发送账号（REQ-20260129-004-friendly-login-prompts）。

## 0.2.0 — 2026-01-25
- 增加可选的论坛 Topic（主题）路由，可将指定用户映射到控制群的对应 Topic，同时保留默认 General 主题的推送行为（REQ-20260125-002-topic-routing）。
- 修复控制群引用 blockquote 出现多余空行的问题（REQ-20260125-003-reply-blockquote-regression）。
- 刷新 README 的亮点与功能列表，补充 Topic 路由等能力说明（REQ-20260125-004-readme-refresh）。
- 将已完成的需求文档归档到 `docs/requests/Done/`，保持活跃需求列表简洁（REQ-20260125-005-archive-done-requests）。
- 修复心跳调度逻辑，使 run 模式下的 “Watcher is still running” 能按空闲间隔重复发送（REQ-20260125-006-heartbeat-repeat）。
- 在启用 Topic 路由时，按用户拆分 HTML 报告并发送到对应 Topic（REQ-20260125-007-topic-report-split）。
- 更新 README 安装 tag 与配置提示以匹配 v0.2.0（REQ-20260125-008-readme-release-tag）。

## 0.1.2 — 2026-01-24
- 修复 run 模式汇总循环未传递 activity tracker 与 Bark 标签的问题，恢复 Bark/控制群通知与 “Watcher is still running” 心跳流（REQ-20260124-024-run-notify-regression）。
- 新增异步回归测试，确保 run 汇总继续传递 tracker/bark 上下文（REQ-20260124-024-run-notify-regression）。

## 0.1.1 — 2026-01-24
- 引入发布管理流程：每个需求选择语义化版本号、更新 changelog，并在 README 中链接日志（REQ-20260124-023-versioning-log）。

## 0.1.0 — 2026-01-23
- 交付 telegram-watch MVP：基于 Telethon 的监听器，支持登录、用户筛选、SQLite 持久化、媒体归档与 HTML 报告推送到控制群（REQ-20260117-001-mvp-bootstrap）。
- 增加 `doctor`/`once`/`run` 三个 CLI 命令，FloodWait 处理、Bark 通知、保留清理，以及引用上下文抓取（REQ-20260117-001-mvp-bootstrap）。
- 发布详尽的配置指南（README + `docs/configuration.md`），覆盖 API 凭据、Chat ID、本地路径与隐私说明（REQ-20260117-002-config-docs）。
