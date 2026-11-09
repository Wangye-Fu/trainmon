# TrainMon

轻量训练监控：训练脚本加一行 `attach()`，自动在训练服务器上拉起 agent，浏览器查看进度、剩余时间、loss 曲线，训练完成/失败/卡住时提醒。

- **不反复 SSH 轮询**：agent 常驻训练服务器，本地增量读取日志
- **训练进程几乎零开销**：只写 manifest / 入队 JSONL，不在训练线程做网络 I/O
- **agent 独立进程**：训练 OOM/被杀后仍能识别失败并保留结果
- **零第三方依赖**：纯标准库 + 原生前端，可完全离线部署
- **兼容 Python 3.6+**
- **SSH 配置集中管理**：`trainmon.ini` 描述服务器，`deploy / start / tunnel` 一条命令完成
- **可控成本**：面板一键停止监控；支持空闲自动退出；停 agent 不会终止训练

## 架构

```text
训练脚本 train.py
  └─ trainmon.attach()                 # 1 行
       ├─ 写 ~/.trainmon/runs/<id>.json
       ├─ 自动确保 host agent 启动（每台机器一个）
       └─ report()/logging → 本地 metrics.jsonl / train.log

host agent（独立进程，常驻）
  ├─ 增量 tail 日志和 metrics
  ├─ 解析 step/loss/epoch，计算 ETA、卡住/完成/失败
  └─ HTTP + SSE 127.0.0.1:8787

本地浏览器（经 SSH 隧道）
  └─ http://127.0.0.1:8787/
```

## 快速开始

### 1. 本地安装

```bash
git clone https://github.com/Wangye-Fu/trainmon.git
cd trainmon
python3 -m pip install --user .
```

也可以直接从源码运行，不安装：

```bash
export PYTHONPATH=/path/to/trainmon:$PYTHONPATH
python3 -m trainmon --help
```

### 2. 创建服务器配置

```bash
trainmon config init
```

会生成 `trainmon.ini`（已加入 `.gitignore`，不会被提交）。编辑：

```ini
[server:gpu-01]
host = gpu-01
remote_dir = ~/trainmon
python = python3
local_port = 8787
remote_port = 8787
```

如果 `gpu-01` 已经写在你的 `~/.ssh/config` 里，`user / port / identity_file` 都可以不填。也可以写 IP 或 `user@host`，但推荐使用 SSH 别名：

```sshconfig
Host gpu-01
    HostName 10.0.0.10
    User your-user
    IdentityFile ~/.ssh/id_ed25519
    # ProxyJump bastion
```

`trainmon.ini` 的完整字段见下方“配置参考”。

### 3. 部署到服务器

```bash
trainmon servers                 # 检查配置
trainmon deploy gpu-01           # 拷贝 + pip 安装 + doctor 自检
```

`deploy` 需要本地有源码；请在仓库目录执行，或显式指定：

```bash
trainmon deploy gpu-01 --source /path/to/trainmon
```

`deploy` 通过 SSH 把项目打包传到 `remote_dir`，然后在远端执行：

```bash
python3 -m pip install --no-deps --no-build-isolation --upgrade --force-reinstall .
python3 -m trainmon doctor
```

没有外网也可以，因为 trainmon 没有第三方依赖。

### 4. 启动训练并打开面板

训练脚本顶部加：

```python
import trainmon

trainmon.attach(
    run="exp-001",
    log="train.log",
    total_steps=20000,
)
```

启动训练时 agent 会自动拉起。也可以手动确保 agent 常驻：

```bash
trainmon start gpu-01
```

本地开隧道：

```bash
trainmon tunnel gpu-01
```

浏览器打开：

```text
http://127.0.0.1:8787/
```

根路径会列出该服务器上的所有训练任务，点击卡片查看详情。`?run=...` 只是可选的深链接。

## 配置参考

`trainmon.ini` 使用 INI 格式（零依赖，兼容 Python 3.6）。查找顺序：

1. `--config PATH`
2. 环境变量 `TRAINMON_CONFIG`
3. 当前目录 `trainmon.ini`
4. `~/.trainmon/config.ini`

```ini
[general]
# 多台服务器时可指定默认项，命令里就可以省略 server 名
# default = gpu-01

[server:gpu-01]
# SSH 主机、IP 或 ~/.ssh/config 别名（必填）
host = gpu-01

# 以下均可选；不填则使用 ~/.ssh/config 和当前系统用户
# user = your-user
# port = 22
# identity_file = ~/.ssh/id_ed25519
# extra_ssh_args = -o ProxyJump=bastion

# 远端安装目录
remote_dir = ~/trainmon

# 训练使用的 Python 解释器（conda/venv 请填绝对路径）
python = python3

# 本地隧道端口 / 远端 agent 端口
local_port = 8787
remote_port = 8787

# 无活跃训练和浏览器连接 N 秒后 agent 自动退出；0=永不
idle_exit = 0
```

多台服务器：

```ini
[general]
default = gpu-01

[server:gpu-01]
host = gpu-01
local_port = 8787

[server:gpu-02]
host = gpu-02
local_port = 8788
remote_dir = ~/trainmon
```

## 训练脚本接入

最小接入（只写 logging）：

```python
import logging
import trainmon

logging.basicConfig(filename="train.log", level=logging.INFO, filemode="w")

trainmon.attach(
    run="exp-001",
    log="train.log",          # 可省略：自动从 logging 的 FileHandler 推断
    total_steps=20000,        # 有总数时 ETA 更准
    stall_seconds=300,
)

for step in range(1, 20001):
    loss = train_step()
    logging.info("step %d/%d loss=%.4f", step, 20000, loss)

trainmon.finish("completed", 0)
```

补充精确指标（可选，不阻塞训练）：

```python
trainmon.report(step=step, loss=loss, lr=lr, extra_metric=0.123)
```

- `attach()` 只做注册和一次 agent 启动检查，毫秒级。
- `report()` 只写本地 `metrics.jsonl`，队列满时丢弃，不做网络 I/O。
- 不调用 `report()` 时，agent 用内置正则解析 `step x/y loss=...`，同样能显示进度和 ETA。
- 不调用 `finish()` 时，进程退出后 agent 会读退出状态并扫描日志判断 completed/failed/lost。
- **每个 run 使用独立日志路径**；并发训练不要共用同一个日志文件。
- DDP/torchrun：`attach()` 读取 `RANK`/`LOCAL_RANK`/`SLURM_PROCID`，只在 rank0 注册，其他 rank 自动 no-op。

### 零侵入包装（可选）

```bash
python3 -m trainmon run --total-steps 20000 train.py
```

注意：wrapper 会在脚本前安装日志 handler，可能改变脚本 `logging.basicConfig` 的落盘位置；优先推荐在脚本里显式 `attach()`。

## 面板功能

- 任务卡片：状态、进度条、step/total、**剩余时间、预计完成时刻、区间、置信度**、速度、loss
- 详情：loss 曲线、完整统计、错误信息、日志尾部
- **下载完整日志**：流式下载远端原始文件
- 实时推送：SSE；断线自动重连，失败时降级 3 秒轮询
- 完成/失败/卡住提醒：页内卡片 + 浏览器通知 + 提示音
- **停止监控**：只停 agent，不终止训练

### 剩余时间怎么算

- 使用最近 120 秒（最多 80 个采样点）的 `(时间, step)` 做最小二乘回归，取斜率作为步速。
- 根据回归残差计算 95% 区间；日志时间戳只有秒级时保留最小不确定度。
- 没有总步数时依次降级：日志 `percent` → `epoch/total_epochs`；都没有则明确提示无法估算。
- 置信度：`高 / 中 / 低`，由 R² 和斜率相对误差共同决定。

## 状态

| 状态 | 含义 |
|---|---|
| `waiting` | 刚启动，还没有解析到进度 |
| `running` | 正常推进 |
| `stalled` | 进程还在，但超过 `stall_seconds` 没有日志/进度 |
| `completed` | 正常结束（退出码 0 或完成标记） |
| `failed` | 非零退出、异常或 OOM/Traceback |
| `lost` | 进程消失且没有退出状态（常见于 SIGKILL/OOM） |

## 成本与停止监控

agent 实测常驻开销约 **17 MB 内存、0.2% CPU**，不占 GPU，没有持续外网流量。相对 GPU 实例费用可以忽略。

但计费方式要注意：

- 按**实例开机时长**计费：停止 agent 不会停止计费，仍需关机/释放实例。
- 按**容器/作业**计费：确认 agent 不是让容器保持存活的唯一进程。
- 训练结束实例自动关机时，agent 会随实例一起消失。

面板右上角和详情页都有 **“停止监控”** 按钮：

- 只停止 agent 的日志采集和面板服务
- **不会终止训练进程**
- 重新查看时：

```bash
trainmon start gpu-01
```

命令行等价：

```bash
trainmon stop gpu-01
```

### 空闲自动退出

```bash
# 在配置文件中设置 idle_exit = 1800
# 或启动时临时指定
TRAINMON_IDLE_EXIT=1800 trainmon start gpu-01
```

规则：

- 默认 `0`，永不自动退出
- 只要还有 `running/waiting/stalled` 任务，或训练 PID 仍存活，就不会退出
- 浏览器页面保持打开时也不会退出
- 退出后历史状态仍在 `~/.trainmon`，下次 `trainmon start` 重新加载

> 配置文件里的 `idle_exit` 由 `trainmon start` / `trainmon restart` 注入。
> 如果你是靠 `attach()` 自动拉起 agent，请在训练服务器上设置环境变量
> `TRAINMON_IDLE_EXIT=1800`，再启动训练。

## CLI

```bash
trainmon config init [--path PATH]     # 创建配置文件
trainmon config path                    # 显示当前使用的配置
trainmon config show                    # 显示解析后的服务器配置
trainmon servers                        # 列出所有服务器

trainmon deploy [server]                # 部署/更新远端代码
trainmon start [server]                 # 启动远端 agent
trainmon stop [server]                  # 停止远端 agent
trainmon restart [server]               # 重启远端 agent
trainmon status [--server NAME]         # 本地或远端查看任务
trainmon tunnel [server]                # 打开 SSH 隧道
trainmon open [server]                  # 打印本地面板地址
trainmon doctor [server]                # 自检

# 本地 agent（一般不需要手动操作）
trainmon agent start|stop|restart|status
trainmon url [--run RUN_ID]
trainmon parse <logfile>
trainmon run [--total-steps N] <script.py>
```

`tunnel` 常用参数：

```bash
trainmon tunnel gpu-01 --local-port 8787
trainmon tunnel gpu-01 --background     # 需要 SSH 公钥/agent，密码登录请用前台
trainmon tunnel gpu-01 --dry-run        # 只打印 ssh 命令
```

### 更新已部署版本

```bash
cd /path/to/trainmon
trainmon deploy gpu-01
trainmon restart gpu-01
```

`restart` 会让远端 agent 重新加载新代码；正在运行的训练不会被终止。

## 多服务器

当前设计是**每台训练服务器一个 agent / 一个面板**，没有中心化云端平台。开多条隧道到不同本地端口即可：

```bash
trainmon tunnel gpu-01 --local-port 8787
trainmon tunnel gpu-02 --local-port 8788
# http://127.0.0.1:8787/ 看 gpu-01
# http://127.0.0.1:8788/ 看 gpu-02
```

如果需要“一个页面看所有机器”，需要再加一个本地聚合 hub：通过多条 SSH 隧道订阅各 agent 的 `/api/events`，合并成统一列表。当前版本未内置。

## 离线 / conda / venv

### 远端没有 pip

trainmon 是纯标准库包。让训练解释器能找到源码目录即可：

```bash
export PYTHONPATH=/path/to/trainmon:$PYTHONPATH
python3 train.py
```

`attach()` 拉起的 agent 会继承该环境变量。

或者写一个 `.pth` 文件：

```bash
SITE=$(python3 -m site --user-site)
mkdir -p "$SITE"
echo "/path/to/trainmon" > "$SITE/trainmon.pth"
python3 -c 'import trainmon; print(trainmon.__version__)'
```

### 使用 conda / venv

trainmon 必须装在训练实际使用的解释器里：

```bash
conda activate train
python -m pip install --user .
```

配置文件中 `python` 填该解释器的绝对路径，`deploy` 和 `start` 都会使用它：

```ini
python = /path/to/miniconda3/envs/train/bin/python
```

## 开发与测试

仓库自带一个合成数据的小 MLP，用于验证监控：

```bash
python3 demo/train_demo.py --steps 300 --sleep 0.05
```

可选参数：

```bash
# 只写 logging，不调用 report()
python3 demo/train_demo.py --steps 300 --no-report

# 模拟失败
python3 demo/train_demo.py --steps 120 --fail-at 60

# 模拟卡住 12 秒（阈值 5 秒）
python3 demo/train_demo.py --steps 120 --stall-at 60 --stall-for 12 --stall-threshold 5
```

运行单元测试：

```bash
python3 -m unittest discover -s tests -t . -v
```

打包：

```bash
python3 setup.py sdist
# 产物在 dist/trainmon-0.1.0.tar.gz
```

## 故障排查

1. `trainmon doctor gpu-01` 检查远端 agent、日志路径、状态。
2. `trainmon parse <log>` 检查解析器能否识别 step/loss。
3. `trainmon tunnel gpu-01 --dry-run` 检查 SSH 命令。
4. 远端 `curl http://127.0.0.1:8787/api/health` 检查 agent。
5. 端口被占用时 agent 会自动尝试 8788–8806；用 `trainmon open gpu-01` 或远端 `python3 -m trainmon url` 查看实际端口。
6. 面板打不开：确认本地隧道进程还在、远端 agent 在跑、`local_port` / `remote_port` 一致。
7. 训练很快结束看不到进度：把 `--sleep` 调大，或查看已完成卡片和历史曲线。

## 已知限制

- 声音/系统通知依赖浏览器页面打开；页面关闭后只能看历史状态。
- 当前没有中心化多机面板，多服务器需要多条隧道/多个标签页。
- 容器/Slurm 作业结束时 agent 可能随环境消失；状态已落盘，下次启动可恢复。
- 训练脚本用 `os._exit`/SIGKILL 终止时无法写退出状态，agent 会降级为 `lost` 并根据日志猜失败原因。
- 历史 manifest 记录的是旧日志绝对路径；迁移历史到新服务器后，旧日志下载可能 404。

## 安全

- agent 默认只绑定 `127.0.0.1`，不对外暴露。
- 通过 SSH 隧道访问，复用现有 SSH 认证；建议只用公钥登录。
- **不要把真实 `trainmon.ini` 提交到 Git**，它已加入 `.gitignore`。
- 本项目不保存 SSH 密码；`deploy` / `tunnel` 直接调用系统 `ssh`，认证交给 ssh-agent、密钥或 `~/.ssh/config`。
- 远端只读日志和状态文件，不需要 sudo。

## 发布到 GitHub

```bash
cd /path/to/trainmon
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/Wangye-Fu/trainmon.git
git push -u origin main
```

- `trainmon.ini` / `*.local.ini` / `.env` 已在 `.gitignore` 中，不会被提交。
- `dist/` 也被忽略；发布 Release 时把 `dist/trainmon-0.1.0.tar.gz` 作为附件上传即可。
- 如果修改了版本号，记得同步 `setup.py` 和 `trainmon/paths.py` 里的 `VERSION`。

## License

MIT
