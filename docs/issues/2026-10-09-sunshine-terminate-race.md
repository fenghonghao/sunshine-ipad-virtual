# Sunshine：undo 执行期间并发 terminate()，导致崩溃或卡死

记录日期：2026-10-09。状态：我们这边已缓解；Sunshine 的 bug 未修，也未上报。

标注约定：**[核实]** 表示有日志或源码为证；**[推测]** 表示没有验证。

## 环境

- Sunshine 2026.914.233613（commit 63d35f7），作为 systemd 用户服务 `app-dev.lizardbyte.app.Sunshine.service` 运行，`Restart=on-failure`
- Ubuntu GNOME Wayland，`capture = kms`，`encoder = vaapi`
- `sunshine.conf` 中配置：`global_prep_cmd = [{"do":"…/bin/phantomctl session-start","undo":"…/bin/phantomctl session-end"}]`
- 我们的控制器（`phantom/controller.py`）在 Sunshine 日志中看到 `CLIENT DISCONNECTED` 后，会调用 `POST /api/apps/close`（closeApp）关闭应用，让 Sunshine 执行 undo

## 现象

| 时间 | 结果 |
|---|---|
| 14:58:39 | Sunshine `SIGSEGV`（core dump），systemd 5 秒后自动重启 **[核实]** |
| 15:44:28 起 | Sunshine 没有崩溃，但之后每次连接都失败，iPad 显示 connection error。15:52 手动重启后恢复 **[核实]** |
| 16:56:09 | 在第一步缓解之后再次 `SIGSEGV`。当时在测"会话中 kill -9 控制器"，详见下文"第一步不够" **[核实]** |

15:44 之后，每次连接的日志都只有下面两行：

```
Info: Display device configuration is disabled. Reverting any active display device configuration.
Error: Property tree is empty. Probably, control flow got interrupted by an unexpected C++ exception. …
```

两行之后没有 `Executing Do Cmd`，也就是说，异常发生在执行 do 命令之前。

## 两次故障的共同时间线 [核实]

| | 14:58 那次 | 15:44 那次 |
|---|---|---|
| `CLIENT DISCONNECTED` | 14:58:31.67 | 15:44:24.63 |
| 第 1 次 undo（来自我们的 closeApp） | 14:58:32.02 | 15:44:25.03 |
| Moonlight 新的 `/launch` 到达，`configure_display` 打出日志 | 14:58:35.41 | 15:44:27.53 |
| 第 2 次 undo（来自 `launch → execute() → terminate()`） | 14:58:35.41 | 15:44:27.53 |
| 控制器处理完两个 session-end | 14:58:35.73 / 35.74 | 15:44:28.72 / 28.74 |
| 结果 | 14:58:39.70 SIGSEGV | 15:44:28.75 `Property tree is empty`，之后一直失败 |

第 1 次 undo 阻塞了大约 3.5 秒。原因是当时控制器要做完锁屏、拔假显示器、等 Mutter 恢复实体屏、恢复排列这几件事，之后才回复 phantomctl。新的连接正好落在这 3.5 秒里。

## 根因

对照的源码版本为 63d35f7：

- **[核实]** `proc_t::terminate()`（process.cpp:322–370）一开始就把 `placebo` 置为 false，并清空 `_process`，然后才逐条执行 undo，并用 `child.wait()` 等待每条命令结束。因此在 undo 执行期间，`running()`（process.cpp:287）已经返回 0：serverinfo 显示 `SUNSHINE_SERVER_FREE`，`launch` 也会放行。
- **[核实]** `launch`（nvhttp.cpp:1331）会调用 `proc::proc.execute()`，而 `execute()` 的第一步就是 `terminate()`（process.cpp:154，注释写着 "Ensure starting from a clean slate"）。
- **[核实]** closeApp 运行在 confighttp 线程，launch 运行在 nvhttp 线程，而 `proc_t` 没有任何互斥锁（process.cpp 中找不到 mutex 或 lock_guard）。结果是两个线程同时执行 `terminate()`，同时操作 `_app_prep_it` 和 `_pipe`。
- **[推测]** 并发执行之后，`_app_prep_it` 被重复递减而越界：一次直接导致段错误（14:58）；另一次在后续每个 `execute → terminate` 里抛异常，Sunshine 一直卡死（15:44）。

## 为什么我们的方案会触发它

这个竞态要三个条件同时满足。如果只是普通地串流实体屏，又不配 undo，三个条件几乎凑不齐：

| 条件 | 普通用法 | 我们的方案（修复前） |
|---|---|---|
| 有人调用 `terminate()` | 只在 Moonlight 里"退出应用"或网页里点"关闭"时才会；普通断开不会 | 每次断开，控制器都会调用 closeApp |
| `terminate()` 持续的时间 | 没有 undo 时是微秒级 | 要等 undo 执行完，约 3.5 秒 |
| 这段时间里来了新的 launch | 几乎不可能 | iPad 断开后很快重连，容易撞上 |

## 我们的缓解（已部署）

`controller.py` 的 `on_client`：收到 `session-end` 时**先回复 `ended`，再执行**锁屏、拔线、恢复排列。

- 这样 phantomctl 立即退出，`terminate()` 的窗口从约 3.5 秒缩短到一次 phantomctl 往返的时间。实测 `phantomctl status` 往返 0.02 秒，session-end 只会更快。
- 如果这时 iPad 马上重连，session-start 会在控制器的 socket 里排队，等清理做完再处理，所以先后顺序不会乱。
- 测试：`test_session_end_replies_before_the_slow_cleanup`（先确认失败，再修到通过）。全部 42 个测试通过。

**第一步不够 [核实]：** 16:56 测试"会话中 kill -9 控制器"时又崩溃了一次。新控制器启动后请 Sunshine 关闭应用（16:56:03.59，undo #1），但主线程紧接着执行启动时的对照：拔线并等 Mutter 恢复实体屏，一直阻塞到 08.36。排队的 session-end 到 08.57 才被读到。iPad 在 05.48 重连，触发 undo #2，09.81 发生 SIGSEGV。"先回复"只在控制器空闲时才有效。

**第二步（已部署）：** `bin/phantomctl session-end` 只把命令写进 socket 就退出，不再等回复。UNIX socket 的排队保证了它和之后 session-start 的先后顺序；session-start 仍然要等，因为 Sunshine 必须等假显示器插好再开始抓图。这样，无论控制器在忙什么，undo 都能马上结束。测试：`test_phantomctl.py::test_session_end_does_not_wait_for_a_busy_controller`，用一个只监听、从不回复的 socket，先确认失败，再修到通过，全部 43 个测试通过。现场：16:58:03 一次 closeApp，undo 用时 15 ms。

**第一步之后的现场数据 [核实]：** 15:52–16:15 之间，iPad 共发起 19 次启动、17 次连接并断开。undo 之后最快 1.9 秒（16:13:38.53 → 40.41）就有下一次启动，大多在 1.9–3.4 秒之间，而修复前，这样的间隔一定会撞进 3.5 秒的窗口。结果是每次断开只执行了一次 undo，`Property tree is empty` 出现 0 次，Sunshine 没有重启过（`NRestarts=0`）。

## 残余风险

- 窗口还剩约 15 ms（一次 phantomctl 启动、连接、写入的时间），没有消除。真正的修复要在 Sunshine 里给 `proc_t` 加锁，或者在 undo 执行完之前，让 `running()` 不要返回 0。
- **[核实]** PR #4390 已把 do 命令处的 `child.wait()` 改成不抛异常的写法（process.cpp:240，`child.wait(ec)`），但 undo 处仍在用会抛异常的 `child.wait()`（process.cpp:347）。
- **[推测]** 其他线程调用 `running()` 时，会执行 `waitpid(-1, WNOHANG)` 回收所有子进程。如果它抢先回收了 undo 子进程，这里的 `child.wait()` 就会抛异常，这是另一条可能产生同样报错的路径。

## 恢复办法

如果再出现 connection error，并且 Sunshine 日志里有 `Property tree is empty`，执行：

```
systemctl --user restart app-dev.lizardbyte.app.Sunshine.service
```

Sunshine 启动时需要一块亮着的屏幕，服务单元里的 ExecStartPre 会先唤醒屏幕。

## 社区情况（2026-10-09 检索）

- **#4461** "Using Do/Undo Command makes connection impossible"：未关闭，最后一条评论在 2026-08-29。症状相同：Linux、配了 do/undo、`Property tree is empty`、只能重启。没有人分析出根因，也没有人提到并发 terminate。是否和我们是同一个原因，未知。
- **#4038**（已关闭）：症状同样是 Malformed XML，由 PR #4390（2025-11-12 合并）修复，原因是 do 命令处的 `split_unix` 和 `child.wait()` 会抛异常。
- 是否在 #4461 发评论：待定。如果发，只说"我们找到了一条能产生同样报错的具体路径"，不说"这就是你们的原因"。

## 同期的其他现象

- 16:13–16:15 之间，大多数会话只持续 1–2 秒，原因是用户自己在 iPad 上反复连接、断开（用户 16:52 确认）。这正好是对缓解措施的一次压力测试，结果见上面的"修复后的现场数据"。期间 16:14:03 出现过一次 `Failed to verify RTSP message tag`，只有这一次，未影响后续连接。
- 16:14:25 和 16:14:36 两次启动的是 apps.json 里自带的 "Low Res Desktop"。它的 do 命令 `xrandr --output HDMI-1 --mode 1920x1080` 在 Wayland 下以退出码 1 失败，Sunshine 随即执行 undo，表现正常。

详细过程见 `.superpowers/sdd/2026-10-08-phantom-display/progress.md` 中的 Task 7 和 Task 5g。
