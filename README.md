# 知栖校园资源服务平台

依据课程任务书实现的课程项目：M1 自习座位预约、M2 整教室申请审批、M3 物业报修，以及三者共用的账号权限、空间目录、通知和审计服务。自然语言 Agent 是附加项；核心平台不依赖 DSH。
FDU工程伦理小组-29作业

[课程项目变更与验收报告](docs/课程项目变更与验收报告.md) 汇总本次修复、双数据库验证、复跑方法和小组待办。

需求索引见 [需求基线](docs/requirements.md)，课程任务书拆解见 [详细需求文档](docs/requirements-detailed.md)，按当前产品实现展开的 [产品需求规格说明书](docs/校园资源平台需求规格说明书.md)，当前架构提案见 [系统结构](docs/architecture.md)，共享数据的实体、权限、冲突规则和变更流程见 [共享信息约束与管理](docs/shared-data-governance.md)，代码中的实际实现方式见 [共享信息约束实现说明](docs/共享信息约束实现说明.md)。

主要文件夹和代码入口见 [仓库目录说明](docs/仓库目录说明.md)。

## 现在能演示什么

- 学生登录后查找和预约座位，查看历史、复用条件、签到或取消。
- 管理员维护房间、座位、角色权限和最长预约时长；最长时长默认 4 小时，可由管理员调整。
- 教室申请经过两级审批；最终通过后才锁定全室座位。
- 报修可派单、维修、验收和评价；维修开始时占用相关资源，验收后释放。
- 屏幕使用仅绑定一个房间的专用令牌展示每日签到码。
- worker 处理预约提醒、未签到释放、申请超时、维修超时升级；配置 SMTP 后可投递邮件。
- 可选 Agent 的写操作接口要求先预览，再由用户明确确认。

当前代码由 coding agent 生成并经过自动化行为测试；**组员尚未逐项人工审查、签收，也未形成启动评审决议**。这些事实需要在 2026-11-05 的小组讨论中确认，不能把 AI 生成或自动测试说成人工验收。

## 本地运行

需要 Python 3.12 和 uv。首次运行：

```sh
uv sync --frozen
uv run alembic -c backend/alembic.ini upgrade head
uv run uvicorn campus_app.main:app --app-dir backend --reload
```

浏览器打开 <http://127.0.0.1:8000>。开发环境默认使用 `data/campus.db`，并在完成数据库迁移后写入虚构演示数据。演示账号密码均为 `demo2026`：`student`、`approver1`、`approver2`、`repair`、`admin`。接口说明见 `/api/docs`。

M2 终审需要 D 资源服务。另开一个终端，使用与主服务相同的数据库，在本机回环地址启动：

```sh
uv run uvicorn campus_app.resource_service:app --app-dir backend --host 127.0.0.1 --port 8001
```

Compose 会同时启动主服务、资源服务、worker 和数据库。本机单独启动时，将 SMTP 和数据库配置导出为进程环境变量。

异步业务任务需单独启动：

```sh
PYTHONPATH=backend uv run python -m campus_app.worker
```

邮件提醒还需要在启动 API 和 worker 前设置 SMTP 环境变量（Compose 会读取 `.env`）。没有邮件服务时，站内通知仍可用。屏幕签到页为 `/screen.html?room_id=LIB-1`，需由管理员先在系统管理页签发房间屏幕令牌并配置到该屏幕浏览器。

## 检查

```sh
uv run ruff check backend
uv run ruff format --check backend
uv run pytest -q
DATABASE_URL=sqlite:////tmp/campus-migration-check.db uv run alembic -c backend/alembic.ini upgrade head
```

默认的业务测试使用独立 SQLite 文件。设置 `TEST_POSTGRES_URL` 为专用 PostgreSQL 测试库的 SQLAlchemy 连接串（使用 `postgresql+psycopg` 驱动）后，运行同一条 `uv run pytest -q` 即可复测。每个用例在新建的独立 schema 内从空库执行 Alembic 迁移，用毕移除该 schema；测试账号需要建 schema 权限。

浏览器验收需先运行 `uv run playwright install chromium`，然后设置 `RUN_BROWSER_TESTS=1` 再执行 pytest。已有 Chrome 时可通过 `CHROMIUM_PATH` 指定可执行文件。验收套件会在回环地址启动真实 HTTP 资源服务和临时 SMTP 收件箱，使用虚构账号测试，不需要外部邮箱。

容器环境和 CI 配置见 `Dockerfile`、`compose.yaml`、`.github/workflows/ci.yml`。GitHub 仓库现为公开。GitHub Actions 已通过代码检查、13 条测试、空库迁移、Compose 测试环境和冒烟流程；[本次运行记录](https://github.com/yushu212/campus-resource-platform/actions/runs/41776105164)。生产部署尚未进行，不要把配置文件说成已部署。

## 汇报与分工材料

- [六人分工与老师可能提问](docs/六人分工与老师提问.md)：分工总表、逐人职责和各自可能遇到的问题。
- [启动评审讲稿与演示问答](docs/kickoff-review.md)：汇报顺序、代码目录说明、演示步骤和常见问题。

## 目录

```text
campus-resource-platform/
├── AGENTS.md                     # 可执行的项目协作规则
├── backend/campus_app/           # FastAPI、M1/M2/M3/D 业务模块与 worker
├── backend/migrations/           # Alembic 数据库迁移
├── backend/tests/                # 业务行为/API 集成测试
├── demo/                         # 浏览器界面与房间签到屏幕
├── docs/                         # 需求、架构、共享数据治理、伦理、分工与交付记录
├── optional-agent/               # 附加 DSH 插件源码（未安装或运行）
├── scripts/                      # 冒烟检查与回滚辅助
├── compose.yaml                  # 本地 PostgreSQL + API + D 资源服务 + worker
└── .github/workflows/            # CI、测试环境启动和人工审批发布模板
```

## 这次启动评审要确认的事

六人真实姓名和模块负责人、公共层 D 的归属及工作量、可选 Agent 是否实施、业务规则中尚未确认的期限/申诉/审批细节、下一次评审时间。候选方案都在 `docs/六人分工与老师提问.md` 与 `docs/decision-record.md`，会议结果留空给组员填写。

项目代码由 coding agent 起草并经过自动化测试；组员尚未逐项人工审查或签收。公开 GitHub 仓库为 `yushu212/campus-resource-platform`；任何人都可以查看，初始提交是供组内审查的 AI 草稿，不代表成员已完成人工贡献。旧版本的远程 CI 已通过。本次变更的远程状态以对应 PR 为准，生产部署待小组执行。
