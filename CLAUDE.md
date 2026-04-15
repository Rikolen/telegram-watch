# Project: telegram-watch (Telegram user-account watcher)

## Non-negotiables

- This project logs in as a Telegram *user account* (MTProto), NOT a Bot.
- Must work on a single Mac (no cloud services required).
- Privacy-first: store data locally, never print secrets, never commit sessions/config with secrets.
- Keep MVP minimal: correctness > features.

## How to run

- Environment setup:
  ```
  python -m venv .venv && . .venv/bin/activate && pip install -e .
  ```
- Configure: `cp config.example.toml config.toml` and fill in api_id/api_hash, target_chat_id, tracked_user_ids, control_chat_id.
- CLI entry point: `tgwatch` (or `python -m tgwatch`)
- Commands: `doctor`, `gui`, `once`, `run`
- GUI launcher: `python -m tgwatch gui --config config.toml`

## Testing / quality gates

- Validation: `python -m tgwatch doctor --config config.toml`
- One-shot: `python -m tgwatch once --config config.toml --since 10m`
- Unit tests: `pytest tests/`

## Safety / compliance (never violate)

- Do not log PII (phone numbers, api_hash).
- Never commit `*.session`, `config.toml`, `data/`, `reports/`.
- `.gitignore` must exclude these at all times.
- Respect Telegram rate limits; implement backoff/floodwait handling.
- Keep data local by default; do not add cloud dependencies unless explicitly approved.

## Repo conventions

- Python 3.11+; type hints.
- Prefer small modules; keep async code isolated.
- Working branch: `dev`; `main` is for release-ready merges.

## README localization rule

- Any change to `README.md` must be applied (or equivalently translated) to all localized READMEs: `docs/README.zh-Hans.md`, `docs/README.zh-Hant.md`, `docs/README.ja.md`.
- Language switch links at the top must stay in sync.

## Todoist workflow (single source of truth for tasks)

本项目使用 Todoist 的 `telegram-watch` 项目（Board 视图）作为**唯一**的任务来源。每次开发活动必须与 Todoist 保持同步。旧的 `docs/requests/**` REQ 文件仅作为历史参考保留，不再创建新文件。

### 看板栏目

| 栏目 | 含义 |
|------|------|
| **Backlog** | 待规划/排期 |
| **In Progress** | 正在开发中 |
| **Code Complete** | 代码完成，等待人工验证 |
| **QA** | 人工验证：`pytest tests/` + `doctor` + 必要时本地实跑 `once` / `run` |
| **Release** | 确认通过，可发布或已发布 |

### 标签与项目边界

- **唯一项目标签**：`telegram-watch`。所有本项目任务必须打上，且只打这一个。
- **不**新建 `Bug` / `商业化` 等分类标签。
- **不**改动其他项目已有的标签/项目（例如 `CodexBar-Mobile`、`Dev` 项目），每次调用 Todoist MCP 必须显式指定项目 id。
- 创建标签/项目前先 `find-labels` / `find-projects`，已存在则复用。

### 任务创建规范

- **必填字段**：content（中文或英文一致即可）、description、labels=`["telegram-watch"]`、priority（`p1`–`p4`）。
- **Release Impact**：每个任务必须在 description 或首条 comment 写明：预计 SemVer bump（`x.y.z → x.y.z+1`）+ 一句话英文 changelog 草稿；纯翻译/typo 可标"无版本影响"。
- **子任务**：预计 >1 天或 >1 PR 的任务必须拆分子任务。
- **Bug 入栏**：线上/P1 故障 → In Progress；P2+ 非紧急 → Backlog。

### 开发流程中的 Todoist 操作

**开始工作时：**
1. 在 Todoist 搜索任务（按标签 `telegram-watch` + 关键词）。
2. 没找到：直接创建新任务，填齐必填字段 + Release Impact，默认进 Backlog。
3. 开始做之前把任务移到 **In Progress**。

**Post-Commit Checklist（每次 `git commit` 之后立刻执行，四步连续不可拆、不可延后）：**

```
git commit → git push → Todoist comment (含 commit 链接) → 任务移到对应栏位
```

comment 正文：
- 日期标记 `[YYYY-MM-DD]`
- 一句话进展摘要（完整变更指向 `docs/CHANGELOG.md`，不复制）
- Commit 链接：`https://github.com/o1xhack/telegram-watch/commit/<sha>`

**代码完成时：**
4. 任务移到 **Code Complete**；comment 说明等待人工验证（如有 PR 附链接）。

**人工验证（QA）通过后：**
5. 本地跑通：`pytest tests/` + `python -m tgwatch doctor --config config.toml`（必要时 `once --since 10m`）。
6. 任务移到 **Release**；添加最终 comment（验证结论 + 版本号）。
7. **由用户确认后**才勾选完成，Claude/Codex **不**自行勾选。

**任务阻塞时：**
- comment 记录阻塞原因和依赖项，任务标题加 `[Blocked]` 前缀。

**会话结束时（跨会话交接）：**
- 未完成任务在 comment 记录：当前状态、下一步、阻塞点。

### Mirror 规则（chat 是主界面）

写入 Todoist comment 的可执行内容（QA 步骤、下一步、commit 摘要），必须同时在 chat 里复述。用户默认不开 Todoist；Todoist 是归档，chat 是实时界面。纯状态 ack（"✅ committed, pushed, Todoist updated"）可以不复述。

### 职责边界

- **Todoist 任务** — 唯一的任务状态 + 进度日志来源；comment 只放摘要 + 链接，不复制完整变更。
- **`docs/CHANGELOG.md`** — 面向开发者的技术变更日志（release 前按 SemVer 追加）。
- **`plan.md`** — 项目计划与功能进度跟踪，由 iSparto 的 Team Lead 维护。
- **`docs/inbox.md`** — 未成型想法的草稿池（和 Backlog 互补：inbox 是想法，Backlog 是已成型任务）。

### 注意事项

- **不要直接勾选完成**：代码完成 ≠ 任务完成，必须走 QA + 用户 approve。
- **状态变动必须移栏**：任务进度变化时同步挪栏位，不能只改 comment 不挪栏。
- **新发现的 Bug**：立即在 Todoist 建任务，带 `telegram-watch` 标签；P1 放 In Progress，P2+ 放 Backlog。
- **QA 发现问题**：任务移回 In Progress，comment 说明问题。
- **Build order for MVP code work**：`doctor` → `once` → `run`（保留自原 REQ workflow）。
- **历史 REQ 存档**：`docs/requests/**`、`docs/templates/REQ_TEMPLATE.md`、`docs/WORKFLOW.md` 作为历史参考保留，不再使用。

## Versioning & changelog

- Every Todoist task must include a "Release Impact" note in its description or first comment, proposing the SemVer bump and changelog snippet (English, App Store style).
- On completion: update version in `pyproject.toml`, prepend entry to `docs/CHANGELOG.md` (newest first).
- SemVer guidance:
  - **Patch**: backward-compatible fixes/docs.
  - **Minor**: additive features, new config surfaces.
  - **Major**: breaking schema/CLI changes.
- Pure typo/translation tweaks skip version bumps and changelog entries.
- README `pip install ...@vX.Y.Z` examples: only update after the new tag exists on GitHub (all languages together).

## Commit protocol

- Always include both a summary and a description:
  - SUMMARY: short, action-oriented, and specific.
  - DETAILS: 2–6 bullets describing key changes, ordered by importance.

---

## iSparto Collaboration Mode

### Role Definitions

| Role | Trigger | Responsibility |
|------|---------|----------------|
| **Team Lead (TL)** | `/start-working`, `/end-working`, `/plan` | Coordinate sessions, create/update `plan.md`, assign work, review results |
| **Worker** | Delegated by TL via Agent tool | Execute a single scoped task (implement, test, review, fix) |
| **Setup Assistant** | `/migrate`, `/env-nogo` | Environment validation, project initialization, migration |

- The **Team Lead** reads `plan.md` at session start to understand current priorities.
- **Workers** operate in isolated scope — they receive a clear task prompt and return results. They do NOT modify `plan.md` or CLAUDE.md.
- When no role is explicitly triggered, default to the standard single-agent mode described above.

### Trigger Condition Table

| User Input | Action |
|------------|--------|
| `/start-working` | TL reads `plan.md`, checks Todoist `telegram-watch` 项目 In Progress / Backlog 栏, proposes today's work |
| `/end-working` | TL summarizes session, updates `plan.md`, lists uncommitted changes, mirrors state to Todoist comments |
| `/plan` | TL produces or updates `plan.md` based on Todoist Backlog / In Progress and current state |
| `/init-project` | Setup Assistant scaffolds a new project |
| `/migrate` | Setup Assistant migrates existing project to iSparto workflow |
| `/env-nogo` | Setup Assistant verifies environment readiness |
| New feature request | Create a Todoist task in `telegram-watch` 项目 Backlog（详见 Todoist workflow 章节） |

### Branching Strategy

- `main` — release-ready, tagged versions only.
- `dev` — active development; all feature work happens here.
- Feature branches (`feat/<slug>`) — optional, for large or multi-session work that shouldn't block `dev`.
- Workers spawned in worktree isolation use temporary branches that are merged back or cleaned up.

### Operational Guardrails

1. **plan.md is the session contract** — read it before starting work, update it when scope changes.
2. **One Todoist task at a time** — do not start a second In Progress task until the current one reaches Release (or is explicitly paused with `[Blocked]`).
3. **No silent scope creep** — if implementation reveals extra work, create a new Todoist task in Backlog rather than expanding the current one.
4. **Workers are scoped** — a worker agent should not make changes outside its assigned task.
5. **Safety first** — all existing CLAUDE.md safety rules (no PII, no secrets, rate limits) apply to every role.
6. **Session hygiene** — `/end-working` must be run before context is lost to ensure `plan.md` stays current.
7. **Post-Commit Checklist is non-negotiable** — see Todoist workflow section; every `git commit` triggers the four-step flow.
