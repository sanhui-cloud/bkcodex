# bkcodex

> 鉴于 GPT 说话越来越像古神低语，为了保护个人 SAN 值，决定把它改成自动驾驶——交由主控智能体在后台代为调度和验收。

面向 **AI 智能体（Agent）编排与调度** 的 Codex CLI 后台任务运行器，支持 **Linux** 和 **Windows**。主要用途是让主控智能体（如 Antigravity、Claude Code、Codex CLI、Cursor、Gemini CLI 等）将批量标注、代码开发与维护、材料检查等可拆分的长任务，派发给多个独立的后台 Codex CLI worker 持续执行、自动验收并输出结构化回执。

- **专为 Agent 调用设计**：通过内置标准 Agent Skill（[skills/bkcodex/SKILL.md](skills/bkcodex/SKILL.md)）驱动，主控智能体负责拆解任务、隔离文件边界、入队、启动与复核 `result.json` / `receipt.json`；人工通常只需完成首次环境配置与登录，或按需查看状态与介入。
- **Linux**：后台由 **systemd 用户服务**管理，**tmux** 与 `journalctl` 查看实时进度。关闭 VS Code 或断开 SSH 后，服务器继续执行。
- **Windows**：原生使用 `msvcrt` 文件锁、Win32 进程树管理与 UTF-8 管道。在 AI 智能体、IDE 子命令或 CI 等带作业对象（Job Object）回收机制的环境中，应直接由宿主后台任务托管 `bkcodex worker --name worker-01`；在交互式终端中也可用 `bkcodex start` 拉起脱离后台进程（日志写入 `state_dir/logs/<worker>.log`）。

每项任务使用独立会话，已有进度通过文件和检查点接续，账号与会话记录保存在配置目录。运行环境：Linux 或 Windows、Python 3.11+、Codex CLI。Python 运行时仅使用标准库，零第三方依赖。

## Agent Skill（供智能体直接调用，推荐入口）

**本仓库主要供 AI 智能体调用，而非仅作为手工命令行工具使用。** 仓库内置了标准跨平台 Agent Skill：[skills/bkcodex/SKILL.md](skills/bkcodex/SKILL.md)，方便 **Antigravity、Codex CLI、Claude Code、Cursor、Gemini CLI** 等主控智能体按统一协议编排后台 Codex worker，自动完成从任务拆分到结果验收的闭环。

### 1. 将 Skill 接入你的智能体

按你使用的智能体任选其一，将 `skills/bkcodex` 复制或软链接到对应 skills 目录：

- **项目级工作区**（推荐团队共享）：复制到项目根目录 `.agents/skills/bkcodex/SKILL.md`
- **Codex CLI 全局**：复制到 `~/.codex/skills/bkcodex/SKILL.md`
- **Claude Code 全局**：复制到 `~/.claude/skills/bkcodex/SKILL.md`
- **Antigravity 全局**：复制到 `~/.gemini/antigravity/skills/bkcodex/SKILL.md`

### 2. 智能体接入后会自动做什么

完成下方一次性的环境安装与登录配置后，直接对主控智能体下达高层目标（例如：“使用 `bkcodex` 将这些任务拆分并派发到后台 Codex worker 执行与验收”），智能体会按 `SKILL.md` 规范自动完成：

1. **环境预检**：执行 `bkcodex doctor`，核对工作区、Codex CLI 路径、凭据状态与当前平台运行模式；
2. **无冲突拆分与入队**：为并行 worker 严格划分不重叠的文件范围，编写任务提示词与可选 `payload.json`，通过 `enqueue` / `import` 写入 SQLite 队列；
3. **按平台安全启动 worker**：Linux 下使用 `systemd`（`install` / `start`），Windows 智能体环境下直接由宿主后台任务托管 `bkcodex worker --name <role>`（避免 `start` 脱离进程被 Windows Job Object 回收）；
4. **巡检、验收与接续**：轮询或等待完成后核对 `status`、`result.json`、`receipt.json` 与 `validation.log`，异常时读取 `CHECKPOINT.json` 并通过 `retry` / `resume` 恢复。

## 环境安装与初始化（首次配置）

### Linux

```bash
git clone https://github.com/sanhui-cloud/bkcodex.git
cd bkcodex
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.local.toml
```

### Windows (PowerShell)

```powershell
git clone https://github.com/sanhui-cloud/bkcodex.git
cd bkcodex
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item config.example.toml config.local.toml
```

在 `config.local.toml` 中填写项目工作目录、Codex 路径、模型和并发数，然后初始化并检查环境：

```bash
bkcodex init
bkcodex login
bkcodex doctor
```

- `login` 通过 Codex 的设备授权流程登录；凭据由 Codex 管理，存放在配置的 `codex.home` 中。仓库忽略本地配置、凭据、日志和运行记录。
- **Windows 提示**：如果本地已登录 Codex Desktop，也可将 `codex.home` 指向 `~/.codex` 并把 `codex.binary` 指向对应 `codex.exe`（TOML 中建议使用正斜杠 `/`）。`bkcodex init` 不会覆盖已有的 `config.toml`。

如需手工验证示例任务，可执行：

```bash
bkcodex import examples/tasks.jsonl
bkcodex install
bkcodex start
bkcodex status
```

首次配置 Linux 服务器时，管理员需要启用用户服务的离线运行：

```bash
sudo loginctl enable-linger "$USER"
```

Linux 下 `bkcodex doctor` 的 `linger` 应显示 `yes`；Windows 下 `runtime` 显示 `windows-detached`、`linger` 显示 `not-applicable`。各工作进程会从队列领取不同任务，完成当前任务后继续领取，队列处理完毕后自动退出。

> **Windows 运行模式提示（重要）**：
> - **AI 智能体 / IDE 子命令 / CI runner（优先使用 `worker`）**：这类宿主通常会为每次短命令附加带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows 作业对象（Job Object），`bkcodex start` 退出时会连带回收脱离进程。在此类环境中请**不要调用 `bkcodex start`**，改由宿主的后台任务机制直接托管运行 `bkcodex worker --name worker-01`（需并发时分别托管 `worker-01`、`worker-02` 等）。队列、独占锁、`status`、`stop` / `stop --now` 与 `runs/` 记录行为完全一致，队列清空后自动退出。
> - **交互式终端（Windows Terminal / PowerShell / cmd）**：可直接执行 `bkcodex start` 拉起脱离后台进程，日志自动写入 `state_dir/logs/<worker>.log`，用 `bkcodex logs` 查看。

## 配置入口

默认读取当前目录的 `config.local.toml`。使用其他配置文件时：

```bash
bkcodex --config /path/to/project.toml status
```

| 配置项 | 用途 |
|---|---|
| `project.name` | 项目名称，用于服务和窗口命名 |
| `project.workspace` | Codex 实际工作的目录 |
| `project.state_dir` | 队列、结果、检查点和运行日志目录 |
| `codex.binary` | Codex CLI 可执行文件路径（Windows 支持 `.exe`/`.cmd` 与反斜杠路径） |
| `codex.home` | 本项目专用的登录与会话目录 |
| `codex.model` | 模型名称，示例为 `gpt-6.1-sol` |
| `codex.reasoning_effort` | 思考强度，示例为 `high` |
| `codex.service_tier` | 服务档位，示例为 `priority` |
| `codex.sandbox` | Codex 文件操作范围，默认 `workspace-write` |
| `codex.writable_dirs` | 额外允许写入的目录 |
| `workers.count` | 同时运行的工作进程数，默认 3 |
| `workers.max_attempts` | 每项任务的执行次数上限，默认 2 |
| `workers.task_timeout_seconds` | 单次任务超时，默认 30 分钟 |
| `workers.max_runtime_hours` | 一批后台工作的时长上限，默认 24 小时 |
| `environment` | 可选代理等环境变量 |
| `validation.command` | 项目自定义验收命令 |
| `validation.timeout_seconds` | 自定义验收命令超时，默认 120 秒 |

模型名称和服务档位以实际账号与 CLI 支持情况为准。可通过 `codex.global_args` 配置当前 CLI 版本需要的全局参数。

## 提交任务

一项任务包含唯一编号、提示词和可选载荷。载荷是供任务使用的 JSON 数据。

```bash
bkcodex enqueue --id job-001 --prompt-file task.md --payload payload.json
```

批量文件每行一项任务，使用 `examples/tasks.jsonl` 的格式。提示词文件路径相对清单目录解析。重复提交相同编号与内容会返回已有任务；内容改变时需要新编号。

每项任务使用新会话，提示词正文原样保留。调度器补充任务文件、结果目录、已有检查点和返回格式。数据标注项目可以在提示词中引用已批准的标注规则。

工作进程把 `result.json` 写到本轮目录：

```json
{
  "task_id": "job-001",
  "status": "completed",
  "summary": "清单已整理。",
  "artifacts": ["checklist.md"]
}
```

`completed` 表示任务返回结果并通过配置的检查。`blocked` 表示执行受阻，`needs_input` 表示需要补充材料或决定。完成状态需要至少一个实际工件（相对路径依次按本轮 `run_dir` 与 `workspace` 解析，且必须落在 `state_dir` 或 `workspace` 内）。

任务结果的专业质量由 `validation.command` 和项目复核流程检查。示例验收脚本为 `examples/validate_markdown.py`。验收命令使用参数数组调用，支持 `{task}`、`{result}` 和 `{run_dir}` 三个占位符。

## 查看、暂停和接续

```bash
bkcodex status
bkcodex logs --name worker-01            # Linux 走 journalctl；Windows 读取 logs/worker-01.log
bkcodex logs --name worker-01 --tail 80 -f  # Windows 实时跟踪日志
tmux attach -t bkcodex-demo-worker-01    # Linux tmux 窗口
```

在 tmux 中按 `Ctrl+b` 再按 `d` 离开。`demo` 对应 `project.name`。

```bash
bkcodex stop          # 当前任务完成后暂停领取
bkcodex stop --now    # 立即终止当前运行中的 worker / Codex 进程树并保存运行记录
bkcodex retry job-001 # 把受阻任务重新放入队列
bkcodex resume        # 清除暂停标记并启动
```

也可直接以托管单 worker 模式运行（队列清空后自动退出；**Windows 下由智能体、IDE 子命令或 CI 调度时应优先使用此模式**，避免 `start` 脱离进程被 Windows 作业对象回收）：

```bash
bkcodex worker --name worker-01
```

该模式下输出直接由宿主任务日志接管（不额外写入 `state_dir/logs/<worker>.log`），进度与回执仍可通过 `bkcodex status` 与 `runs/<task>/<invocation>/` 查看，暂停/终止同样使用 `bkcodex stop` 或 `bkcodex stop --now`。

发生网络错误时，工作进程使用有限重试和共享等待时间。登录失效、额度不足或连续三次失败会使对应工作进程保存退出。修复后可用 `retry` 和 `resume` 接续。进程被强制终止时，已写入磁盘的记录保留；下一次启动读取此前的运行目录。

Linux 下 `install` 写入用户服务，`start` 交由 `systemd` 守护进程启动；Windows 交互式终端下 `install` 仅初始化目录，`start` 直接拉起脱离后台进程。

## 文件结构

```text
state_dir/
  queue.sqlite                任务、归属和状态记录
  STOP                        暂停标记
  RUN_WINDOW.json             本批运行时限
  NETWORK_COOLDOWN.json        共享网络退避
  workers/                    工作进程状态和独占锁
  logs/                       Windows 后台 worker 日志（<worker>.log）
  runs/<task>/<invocation>/
    task.json                 本次任务与载荷
    instruction.txt           实际提示词
    events.jsonl              Codex 事件流
    stderr.log                CLI 错误记录
    execution.json            退出状态与 token 用量
    final.txt                 最后回复
    result.json               任务返回结果
    receipt.json              检查通过记录
    CHECKPOINT.json           任务按需保存的接续点
  codex-home/                 专用登录与会话记录
```

运行器从 Codex 返回事件中记录 token 用量。美元费用和套餐额度需要结合实际计费规则统计。

## 运行机制

```mermaid
flowchart LR
    C[TOML 配置] --> S["Linux: systemd / Windows: Detached Worker"]
    Q[任务队列] --> W[并行工作进程]
    S --> W
    W --> E[每项任务的新 Codex 会话]
    E --> A[结果与检查点]
    A --> V[字段检查与项目验收]
    V --> Q
    W --> L[日志]
    L --> T["Linux: tmux / Windows: logs/*.log"]
```

SQLite 事务分配任务，每个工作进程持有独占文件锁（Linux `fcntl` / Windows `msvcrt`）。结果按任务、调用分别保存。代理地址通过配置传入，节点切换由外部代理服务负责。

## 开发与验证

Linux / macOS：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Windows (PowerShell)：

```powershell
$env:PYTHONPATH = "src"; python -m unittest discover -s tests -v
```

测试使用模拟 CLI 检查并发领取、任务恢复、结果验收、超时、停止流程以及跨平台文件锁与 Windows 脱离进程生命周期。GitHub Actions 在 `ubuntu-latest` 与 `windows-latest`（Python 3.11 / 3.13）上自动运行全套测试。

Codex 参数与事件形式参考 [OpenAI 官方非交互运行文档](https://learn.chatgpt.com/docs/non-interactive-mode)。
