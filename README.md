# bkcodex

把 Codex CLI 任务放到服务器后台持续运行。适合批量标注、代码维护、材料检查和其他可拆成独立任务的工作。

后台由 **systemd 用户服务**管理，**tmux** 用于查看实时进度。关闭 VS Code 或断开 SSH 后，服务器继续执行。任务使用独立会话，已有进度通过文件和检查点接续。账号与会话记录保存在专用目录。

运行环境：Linux、Python 3.11+、Codex CLI。后台运行需要 systemd 用户服务；进度窗口使用 tmux。Python 运行时采用标准库。

## 快速开始

```bash
git clone https://github.com/sanhui-cloud/bkcodex.git
cd bkcodex
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.toml config.local.toml
```

在 `config.local.toml` 中填写项目工作目录、Codex 路径、模型和并发数，然后初始化并在独立目录登录：

```bash
bkcodex init
bkcodex login
bkcodex doctor
```

`login` 通过 Codex 的设备授权流程登录。凭据由 Codex 管理，存放在配置的 `codex.home` 中。仓库忽略本地配置、凭据、日志和运行记录。

提交两个示例任务并启动：

```bash
bkcodex import examples/tasks.jsonl
bkcodex install
bkcodex start
bkcodex status
```

首次配置服务器时，管理员需要启用用户服务的离线运行：

```bash
sudo loginctl enable-linger "$USER"
```

`bkcodex doctor` 的 `linger` 应显示 `yes`。各工作进程会从队列领取不同任务，完成当前任务后继续领取。队列处理完毕后自动退出。

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
| `codex.binary` | Codex CLI 可执行文件路径 |
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

`completed` 表示任务返回结果并通过配置的检查。`blocked` 表示执行受阻，`needs_input` 表示需要补充材料或决定。完成状态需要至少一个实际工件。

任务结果的专业质量由 `validation.command` 和项目复核流程检查。示例验收脚本为 `examples/validate_markdown.py`。验收命令使用参数数组调用，支持 `{task}`、`{result}` 和 `{run_dir}` 三个占位符。

## 查看、暂停和接续

```bash
bkcodex status
bkcodex logs --name worker-01
tmux attach -t bkcodex-demo-worker-01
```

在 tmux 中按 `Ctrl+b` 再按 `d` 离开。`demo` 对应 `project.name`。

```bash
bkcodex stop          # 当前任务完成后暂停领取
bkcodex stop --now    # 中断当前进程并保存现有运行记录
bkcodex retry job-001 # 把受阻任务重新放入队列
bkcodex resume        # 清除暂停标记并启动
```

发生网络错误时，工作进程使用有限重试和共享等待时间。登录失效、额度不足或连续三次失败会使对应工作进程保存退出。修复后可用 `retry` 和 `resume` 接续。进程被强制终止时，已写入磁盘的记录保留；下一次启动读取此前的运行目录。

`install` 写入用户服务，`start` 启动本批任务。默认在服务器重启后手动执行 `resume`；需要开机接续时，可自行启用生成的 systemd 服务。

## 文件结构

```text
state_dir/
  queue.sqlite                任务、归属和状态记录
  STOP                        暂停标记
  RUN_WINDOW.json             本批运行时限
  NETWORK_COOLDOWN.json        共享网络退避
  workers/                    工作进程状态和独占锁
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
    C[TOML 配置] --> S[systemd 用户服务]
    Q[任务队列] --> W[并行工作进程]
    S --> W
    W --> E[每项任务的新 Codex 会话]
    E --> A[结果与检查点]
    A --> V[字段检查与项目验收]
    V --> Q
    W --> L[日志]
    L --> T[tmux 进度窗口]
```

SQLite 事务分配任务，每个工作进程持有文件锁。结果按任务、调用分别保存。systemd 管理进程生命周期，tmux 展示日志。代理地址通过配置传入，节点切换由外部代理服务负责。

## 开发与验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用模拟 CLI 检查并发领取、任务恢复、结果验收、超时和停止流程。项目语义检查作为适配层接入。

Codex 参数与事件形式参考 [OpenAI 官方非交互运行文档](https://learn.chatgpt.com/docs/non-interactive-mode)。
