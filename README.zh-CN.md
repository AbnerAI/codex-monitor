# codex-monitor

让 Codex CLI 持续监听文件或命令输出，在有新内容时通知原会话；空闲会话可以被唤醒。
由独立 Python 后台服务、CLI、Codex skill 组成。核心监听服务只用标准库；
可选的 iTerm2 自动显示功能另需独立安装 Python 客户端。

## 安装与使用

需要 macOS/Linux、Python 3.9+、已登录且启用共享 app-server 的兼容 Codex CLI。
当前目标协议为 0.166.0；桌面版独有会话、旧版 CLI、`--no-daemon` 不保证可用。

下载/克隆本仓库后，在仓库根目录执行：

```bash
python3 scripts/install.py
```

默认安装服务到 `~/.local/share/codex-monitor/`，命令到
`~/.local/bin/codex-monitor`，skill 到 `~/.agents/skills/codex-monitor/`。
可加 `--start` 启动服务。安装器不改 Codex 配置，不启用开机启动；已有文件会备份。
若 Codex 的技能目录没有自动刷新，重新打开会话。确保 `~/.local/bin` 在 PATH 中。

随后对 Codex 说：

```text
$codex-monitor 监听 /tmp/inbox.ndjson，有新消息通知当前会话
$codex-monitor 每30秒检查这条命令，输出变化时通知：cat /tmp/job-status.txt
$codex-monitor 查看监听
$codex-monitor 停止 monitor-ID
```

skill 会调用真实服务；它本身不常驻。也可以在当前 Codex 的 shell 工具中执行：

```bash
codex-monitor doctor
codex-monitor start
codex-monitor watch --file /tmp/inbox.ndjson --name inbox
codex-monitor list
codex-monitor events --id monitor-ID
codex-monitor unwatch monitor-ID
codex-monitor stop
```

从其他终端追加完整一行，就会触发通知：

```bash
printf '{"content":"任务已完成"}\n' >> /tmp/inbox.ndjson
```

默认跳过已有内容。`--from-start` 包含旧内容，`--once` 在首批通知被接受后停止。
无结尾换行的半条 JSON 会等写完再处理。命令监听用于周期性执行短查询，不能用
`tail -f` 等永久不退出的命令；用 `--timeout` 限时。

当前会话由 `CODEX_THREAD_ID`、`CODEX_HOME` 精确绑定。在普通终端使用时必须明确
提供 `--thread UUID --codex-home PATH`。全局 `--state-dir PATH` 要写在子命令之前。

## 可选的 iTerm2 监听计数

只统计精确 Codex thread 和规范化后 `CODEX_HOME` 范围内的活跃监听：
两个显示 `monitor-2`，一个显示 `monitor-1`，零个留空。显示位置是 iTerm2 状态栏，
不是 Codex CLI 的原生底栏。

### 新 Codex shell 自动启用

启用后，在新的交互式 zsh 中照常输入 `codex` 即可。只包装这条命令，保留原有
Codex 可执行文件或负责账号、`CODEX_HOME` 选择的入口；不包装 `claude`，不改
Claude Code 配置，也不重新加载已有 shell，不关闭或重启 iTerm2。

先按前面的步骤执行 `python3 scripts/install.py` 安装服务和 skill，然后：

1. 用户在 iTerm2 中手动启用一次 **Settings > General > Magic > Enable Python API**。
   这是独立的全局 API 开关，安装器不会替用户开启；后续布局操作只针对当前终端会话。
   保持默认 API 认证即可，不需要启用 **Allow all apps to connect**（允许所有应用连接）。
   不用重启 iTerm2，也不用关闭已有 shell；自动方式无需改共享 profile 中的
   **Profiles > Session > Status bar** 设置。
   首次连接若出现 macOS 自动化授权提示，允许当前终端或启动器与 iTerm2 通信即可。
2. 为可选客户端创建独立运行环境：

   ```bash
   python3 -m venv ~/.local/share/codex-monitor/iterm-runtime
   ~/.local/share/codex-monitor/iterm-runtime/bin/python -m pip install 'iterm2>=2.30,<3'
   ```

3. 预览将写入的 zsh 代码，再选择安装：

   ```bash
   python3 scripts/install_iterm.py
   python3 scripts/install_iterm.py --install
   ```

在准备使用的当前 iTerm2 shell 中，可以先做只读检查：

```bash
codex-monitor iterm api-status
```

该命令核对当前终端的精确 UUID 和单 pane 条件，只读两项状态栏设置。成功结果
包含 `"api_available": true`，不会修改设置或登记监听。

`python3 scripts/install_iterm.py` 只读预览受管代码块，不输出原有 rc 内容。安装时会备份 `~/.zshrc`，保留
其他内容和文件权限，只给后续交互式 shell 增加函数；已有同名 `codex` 函数或别名
会被保留。不会对现有 shell 执行 source 或重新加载。受管函数生效后，自行打开新
shell 并照常输入 `codex`，每个新的 Codex shell 都不必再手动添加显示组件。
若原先已有同名函数或别名，应自行将下面的显式启动命令接入其中，不要直接覆盖。
在新 shell 中，`whence -w codex` 应显示函数，`whence -p codex` 仍应指向 PATH
上的原可执行入口；直接使用 `codex-monitor` 命令时还需保证 `~/.local/bin` 在 PATH 中。

每次符合条件的本地交互式启动，受限的 iTerm2 API 客户端先保存该终端会话当前的
状态栏布局和启用状态，保留已有组件，只在缺少时追加计数组件，并为本次 Codex
运行启用状态栏。不会改共享 iTerm2 profile 或全局布局偏好。`codex exec`、帮助/
版本查询、远程或 `--no-daemon` 启动、非 TTY 调用，以及其他终端都跳过自动显示。
当前适配器要求 tab 内只有一个 pane；不支持的布局仍会启动 Codex，只跳过可选计数。

`watch` 登记监听后，使用启动器生成的随机 token、仍有效的私有租约、精确终端
UUID，以及当前 Codex thread/profile 自动绑定。token 和终端身份只注入本次
Codex 的工具环境，不拿共享 app-server 之前残留的环境推测目标。没有有效启动
token 时不会擅自自动绑定。显示绑定失败也不会丢弃已经登记好的监听。

正常退出 Codex 时，会清空计数；若布局仍与启动器设置的一致，就恢复进入前保存
的状态栏设置。用户在运行期间自行修改的布局会保留，监听和服务本身不会被停止。
若启动器被 `SIGKILL` 强制杀死，计数清理可能要等终端重新成为当前会话；空白
状态栏可能保留到下一次 Codex 启动修复，不能保证强杀后立即完成清理。

不使用 zsh 函数时，也可以明确指定原入口启动：

```bash
codex-monitor launch --codex /absolute/path/to/original/codex -- resume --last
```

若可选运行环境放在其他位置，用 `CODEX_MONITOR_ITERM_PYTHON` 指定其中的 Python。
移除受管启动代码块用：

```bash
python3 scripts/install_iterm.py --uninstall
```

这不会终止运行中的 Codex、关闭 API、删除运行环境或修改原始 `codex` 入口。
开启 API 后，当前授权 shell 的真实生命周期检查已通过：自动添加组件、精确绑定
当前 thread、显示 `monitor-2`、退出时清空计数并恢复原布局。该检查没有新开
iTerm2 窗口，也没有另起一个新的交互式 Codex TUI。API 关闭时，核心监听服务
和下面的当前会话手动方式仍可正常使用；具体证据见[验收记录](docs/validation.md)。

自动显示排查：

| 现象 | 检查方式 |
|---|---|
| 提示自动计数不可用 | 在目标单 pane shell 中运行 `codex-monitor iterm api-status`，核对 Python API 开关和独立客户端环境；保持默认认证，不需要允许所有应用连接。 |
| 新 shell 的 `codex` 没被接入 | 用 `whence -p codex` 检查 PATH，`whence -w codex` 检查当前定义；已有别名、函数会被刻意保留。受管代码只在后续交互式 zsh 加载，不要重新加载所有运行中的 shell。 |
| 计数为空白 | 没有活跃监听时会留空；启动器要等精确 thread 登记监听后才自动绑定。远程、非 TTY 或多 pane 等不支持的场景会跳过显示。 |

### 当前会话手动设置

此方式不需要开启 Python API，也不依赖可选客户端。可从目标 Codex 会话执行：

```bash
codex-monitor iterm attach
codex-monitor iterm status
codex-monitor iterm detach
```

请从目标 Codex 会话执行，并让对应 iTerm2 会话保持为当前会话。`attach` 精确绑定
thread 和规范化后的 `CODEX_HOME`，核对 iTerm2 UUID 及单 pane 条件后启动计数更新器。
只统计该范围内仍 active 的 watcher：两个显示 `monitor-2`，一个显示 `monitor-1`，
零个留空。切到其他
iTerm2 会话后更新器会暂停，不查找或修改其他 shell；暂停期间旧值可能暂时滞后。
`detach` 停止更新器并还原此前的 `user.codexMonitor` 值，不停止 watcher 或监听服务。
切换 Codex thread 或 profile 后，需要先 detach 再为精确的新目标 attach；不会从
标题、目录或历史记录推测替代目标。

显示组件需要在当前终端选择 **Session > Edit Session > Session > Status bar >
Configure**，按需启用状态栏。将上方 **Status Bar Component Menu** 中的
**Interpolated String** 拖到下方 **Active Components**，选中新加的组件，再点
**Configure Component**；其 String Value 填：

```text
\(user.codexMonitor)
```

使用 **Edit Session** 将改动限制在当前 shell。此手动方式只发布 `user.codexMonitor`，
不修改标题、共享 profile、全局偏好或 API 开关，不操作其他 shell；官方 Codex CLI、
二进制与配置均保持不变。原版 Codex 的原生底栏不支持本集成的自定义计数项。

新开的 iTerm2 tab、pane 或终端会话不会继承手动的 **Edit Session** 设置。
希望照常输入 `codex` 自动配置时，使用上面的可选启动器；也可以自行在已保存或
专用 profile 中配置组件。不使用启动器时，每个新 Codex thread 仍需对精确目标
执行 `iterm attach`；复用已绑定终端时先 `detach` 再 `attach`。当前终端已完成
的手动组件设置仍然有效。

当前变量发布已实测通过“留空 → `monitor-2` → `monitor-1` → 留空”，
标题格式及服务实例均未改变。添加连字符前，在两个测试自有 watcher 活跃的
30 秒可见性检查中，用户明确确认当前授权 shell 中“看到了，monitor2”；
该原始值通过了用户目视确认。
当前显示格式为 `monitor-N`，例如 `monitor-2` 或 `monitor-1`。
可参考 iTerm2 的[状态栏说明](https://iterm2.com/documentation-status-bar.html)和
[当前会话设置范围](https://iterm2.com/documentation-menu-items.html#session-edit-session)。

## 边界与可靠性

- 事件通过已有 Codex app-server 的 Unix WebSocket 和独立 `toolOutput` 投递，
  保留外部工具输出身份，不覆盖目标会话的模型、权限或审批策略。
- 文件偏移与待投递事件使用同一 SQLite 事务保存，重启可续读；文件轮转、截断会重置偏移。
- 等待用户输入/审批、未加载的会话会暂存事件；服务不替用户审批，也不会擅自恢复会话。
- `accepted` 只表示 Codex 接受并返回 turn id，处理是否完成要看原会话回复。
- `uncertain` 表示投递后没有拿到确定回执；不会自动重发。先核对会话历史，必要时
  执行 `codex-monitor retry EVENT_ID`。不声称“严格恰好一次”。
- 命令在后台服务的系统用户权限下执行，不经过目标会话的 shell 沙盒；只登记明确授权、
  适合重复运行的命令。通知后 Codex 执行的工作仍遵循会话自身权限。
- 状态与日志位于私有的 `~/.codex-monitor/`；事件内容会保留，当前没有自动清理历史。
- 服务不读写 Claude Code 配置、技能或运行状态。无需修改 Codex 二进制或注册 MCP。

完整参数、服务托管模板、故障处理和限制见 [English README](README.md)。

## 测试、发布

```bash
python3 -m unittest discover -s tests -v
python3 scripts/package.py
```

生成独立源码包 `dist/codex-monitor-0.1.0.zip` 和技能包
`dist/codex-monitor.skill`。你可以直接将本仓库上传自己的 GitHub；发布包不包含
个人账户、机器路径或运行数据。skill 包仍需单独安装服务。

可选真实验收（使用当前 Codex 账户，产生少量正常模型用量）：

```bash
python3 scripts/session_check.py arm
```

在需要验收的原 Codex 会话处于 active 时，通过其 shell 工具运行 `arm`，随后结束
当前回合。独立观察器确认该精确线程空闲至少 0.5 秒后，才追加随机标记。事件唤醒
原会话后，模型回复 `arm` 返回的确认 token，再查看或复核报告：

```bash
python3 scripts/session_check.py report --directory /path/returned/by/arm
python3 scripts/session_check.py confirm --directory /path/returned/by/arm
```

当前实际会话已通过该验收：报告记录了真实 `agentMessage` 的 item ID，并核对其
返回的 `turnId` 与事件接受的 turn 一致。该检查不创建另一模型会话、不覆盖模型或权限
设置；仅有工具输出回显、WebSocket 握手或 `accepted` 回执不算模型确认回复。

另有独立测试会话与忙碌时投递的检查：

```bash
python3 scripts/live_check.py --mode idle
python3 scripts/live_check.py --mode busy
```

如上游模型暂时不可用，可用 `--mode idle --trigger-only` 单独检查“空闲会话收到事件后
启动新回合”，该模式不声称已验证模型完整回复。当前实测结果见 [验收记录](docs/validation.md)。

MIT 许可证。设计受 [AbnerAI/dsh-monitor](https://github.com/AbnerAI/dsh-monitor) 启发，
为独立重写的社区项目。
