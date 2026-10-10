# sunshine-ipad-virtual

让 iPad 通过 [Moonlight](https://moonlight-stream.org/) 变成 Linux 笔记本的"临时外接屏"。

Moonlight 连接时，内核会插上一块和 iPad 分辨率完全一致的**假显示器**，[Sunshine](https://github.com/LizardByte/Sunshine) 用 KMS 抓取它；断开时再把它拔掉。整个过程和插拔一块真实显示器一样：

- **满屏、无黑边：** iPad Air 13″ 的 2732×2048、4:3 原生分辨率。
- **锁屏和熄屏状态下也能连：** 可以直接在 iPad 上输入密码解锁。
- **两种模式：**
  - `primary`：iPad 是唯一的屏幕，实体屏关闭；
  - `extend`：iPad 作为扩展屏，排在实体屏下方居中。
- **断开后恢复原状：** 拔掉假显示器，恢复你原来的屏幕排列。只有当 iPad 是唯一亮着的屏幕时，才会先锁屏。
- **不碰电源逻辑：** 假显示器只在串流期间存在，合盖、休眠全部交给 logind 和 GNOME，用电池时合盖照常休眠。
- **Secure Boot 可以保持开启：** 不需要 debugfs，也不需要第三方内核模块。

> 这是针对一台具体机器开发并实测的项目，测试环境见下文。硬件（显卡接口、iPad 型号）不同时需要改几个常量，见[适配其他硬件](#适配其他硬件)。

## 目录

- [工作原理](#工作原理)
- [要求](#要求)
- [安装配置](#安装配置)
- [验证](#验证)
- [日常使用](#日常使用)
- [卸载](#卸载)
- [适配其他硬件](#适配其他硬件)
- [开发](#开发)
- [已知限制](#已知限制)

## 工作原理

```
连接
  iPad 上的 Moonlight ──启动 "Desktop"──▶ Sunshine
      Sunshine 执行 global_prep_cmd:  phantomctl session-start ──UNIX socket──▶ 控制器
      控制器: 点亮屏幕 → sudo sunshine-phantom-drm on → 等 Mutter 认出新屏幕 → 应用 primary/extend 布局
      Sunshine 用 KMS 抓取 HDMI-A-1（也就是假显示器）→ iPad 显示画面

断开（任意一种）
  · Sunshine 日志出现 CLIENT DISCONNECTED / Ping Timeout / Failed to verify tag / Bad packet type
      → 控制器通过 Sunshine 网页 API 关闭应用 → Sunshine 执行 undo: phantomctl session-end
  · Sunshine 退出或重启（systemd 的 MainPID 变化）
  · 控制器收到 SIGTERM（注销、systemctl stop/restart），或被 kill -9 后由新启动的进程接手
      → 如果 iPad 是唯一亮着的屏幕就先锁屏 → sudo sunshine-phantom-drm off → 恢复原来的屏幕排列
```

| 组件 | 作用 |
|---|---|
| `root/sunshine-phantom-drm` | 唯一以 root 运行的部分，只接受 `on`/`off`/`status` 三个参数。它会把 `edid/…bin` 写进 `/sys/module/drm/parameters/edid_firmware`，再把 i915 的 `HDMI-A-1` 强制设为已连接。sudoers 规则只允许这三种调用 |
| `phantom/controller.py` | 控制器，以 systemd user service 的形式常驻，所有决策都在这里 |
| `phantom/display.py` | 通过 Mutter 的 `org.gnome.Mutter.DisplayConfig` 读取和应用布局（临时应用，不写入 `monitors.xml`） |
| `phantom/contact.py` | 熄屏时检测 Moonlight 的来访，提前点亮屏幕（原因见下） |
| `phantom/sunshine_api.py` | 调用 Sunshine 网页 API 关闭应用；判断哪些日志行表示串流结束 |
| `bin/phantomctl` | 给 Sunshine 的 `global_prep_cmd` 调用：`session-start`、`session-end`、`status`、`mode` |
| `bin/sunshine-ipad-mode` | 切换 `primary`/`extend` 模式 |
| `make-edid.py`、`edid/` | 生成假显示器用的 EDID：2732×2048@60，CVT-RB2 时序。EDID 里声明为 HDMI 2.0 接收端，否则 i915 会把像素时钟限制在 165 MHz |
| `systemd/` | 控制器服务单元；以及一个 Sunshine 的 drop-in，让屏幕在 Sunshine 启动前先亮起来 |
| `install.sh`、`root/install.sh` | 安装脚本：前者装用户部分，后者用 sudo 装 root 部分 |

几个关键的设计取舍：

- **为什么不用 Mutter 的虚拟显示器？** 锁屏时 gnome-shell 会结束所有屏幕录制会话，导致锁屏状态下没法串流。`obsolete/` 里保留了当初那个方案的脚本。
- **为什么假显示器只在串流期间存在？** 只要它插着，logind 就会认为接着外接屏（`Docked`），用电池合盖时就不会休眠。所以断开后必须拔掉，休眠交回给 logind。
- **为什么要"检测来访并点亮屏幕"？** Sunshine 每次启动后，第一次有人连接时会先在一块亮着的屏幕上检测编码器，然后才执行 `global_prep_cmd`。如果这时所有屏幕都熄灭了，连接就会失败。Moonlight 的主机列表大约每 2 秒访问一次 Sunshine，控制器看到这些访问后就先把屏幕点亮。
- **为什么断开时要通过网页 API 关闭应用？** Sunshine 只有在应用被关闭时才执行 undo 命令。单纯断开连接时，应用会继续运行，等待客户端回来恢复会话。
- **Sunshine 的一个上游竞态问题：** undo 还没执行完时，如果又有新的连接进来，Sunshine 可能崩溃或卡死。详见 [docs/issues/2026-10-09-sunshine-terminate-race.md](docs/issues/2026-10-09-sunshine-terminate-race.md)。为了缩小这个窗口，`phantomctl session-end` 只把命令放进队列就立即返回。

## 要求

| 项目 | 测试环境 |
|---|---|
| 系统 | Ubuntu 26.04 LTS，内核 7.0，systemd 259 |
| 桌面 | GNOME 50.1（Mutter 50.1），**Wayland** |
| 显卡 | Intel Arrow Lake-P 核显（i915），另有 NVIDIA 独显；只用到 Intel 这一块 |
| Sunshine | 2026.914（官方 Ubuntu 26.04 deb 包，单元名为 `app-dev.lizardbyte.app.Sunshine.service`） |
| Python | 系统自带的 `/usr/bin/python3` 3.14 和 PyGObject 3.56（**不需要 pip**） |
| 客户端 | iPad Air 13″ 上的 Moonlight |

硬性条件：

1. **GNOME on Wayland。** 控制器依赖 Mutter 的 D-Bus 接口和 logind。
2. **i915 上有一个后面没有物理插口的接口。** 本机用的是 `HDMI-A-1`，机身上的 HDMI 口接在 NVIDIA 上。用下面的命令查看：

   ```bash
   for c in /sys/class/drm/card*-*; do printf '%-22s %-13s %s\n' "$(basename "$c")" "$(cat "$c/status")" "$(basename "$(readlink -f "${c%%-*}/device/driver")")"; done
   ```

   要找的是驱动为 `i915`、状态为 `disconnected` 的 HDMI 或 DP 接口。如果拿不准，可以把显示器依次插到机身的每个视频口上，看哪个接口变成 `connected`，排除掉这些。
3. **Sunshine 能用 KMS 抓屏。** 执行 `getcap $(readlink -f $(which sunshine))`，输出里应该有 `cap_sys_admin`；如果没有，参考 Sunshine 文档执行 `sudo setcap cap_sys_admin+p $(readlink -f $(which sunshine))`。
4. **有 sudo 权限。** 安装 root 部分时需要一次。

## 安装配置

整个过程是两条安装命令，加上 Sunshine 和 Moonlight 的常规设置。

### 1. 准备 Sunshine

1. 安装 Sunshine，打开它的网页（`https://localhost:47990`），设置用户名和密码，并和 iPad 上的 Moonlight 完成配对。
2. 确认 Sunshine 能用 KMS 抓屏（见[要求](#要求)第 3 条）。

### 2. 获取代码

放在哪里都可以：

```bash
git clone <仓库地址> ~/sunshine-ipad-virtual
cd ~/sunshine-ipad-virtual
```

### 3. 安装 root 部分（只需一次）

```bash
sudo bash root/install.sh
```

这一步会安装三个文件：

| 文件 | 内容 |
|---|---|
| `/lib/firmware/edid/sunshine-ipad-air-13.bin` | 假显示器的 EDID |
| `/usr/local/bin/sunshine-phantom-drm` | root helper |
| `/etc/sudoers.d/sunshine-phantom` | 只允许**当前用户**免密执行 helper 的 `on`/`off`/`status`，会先经过 `visudo -c` 检查 |

helper 是**复制**到 `/usr/local/bin` 的，而不是直接引用仓库里的那一份。原因是安全：免密的 sudo 规则必须指向一个只有 root 能修改的文件，否则任何以你的身份运行的程序，只要改一下脚本内容，就能拿到免密 root。

### 4. 安装用户部分

```bash
bash install.sh
```

脚本会先检查前提条件：root 部分是否已装好、Sunshine 是否已安装、系统有没有 Python GObject 绑定、`sunshine.conf` 能否正确解析。任何一项不满足，它都会说明原因并直接退出，不做任何改动。检查通过后，它会询问 Sunshine 网页的用户名和密码，然后完成下面这些事：

| 做了什么 | 位置 |
|---|---|
| 复制程序文件 | `~/.local/share/sunshine-ipad-virtual/`（`phantom/` 下的模块和 `bin/` 下的命令，不含测试） |
| 把命令链接到 PATH | `~/.local/bin/phantomctl`、`~/.local/bin/sunshine-ipad-mode` |
| 安装控制器服务，并设为随 Sunshine 启动 | `~/.config/systemd/user/sunshine-phantom.service` |
| 让屏幕在 Sunshine 启动前先亮起来 | `~/.config/systemd/user/app-dev.lizardbyte.app.Sunshine.service.d/50-phantom-wake.conf` |
| 保存网页账号密码（权限 600），断开时用来关闭应用 | `~/.config/sunshine-ipad-virtual/credentials` |
| 先备份，再写入 Sunshine 设置 | `~/.config/sunshine/sunshine.conf`（备份文件为 `sunshine.conf.bak-日期时间`） |

写入的 Sunshine 设置如下（另见 [`config/sunshine.conf.example`](config/sunshine.conf.example)）：

| 设置 | 作用 |
|---|---|
| `capture = kms`、`encoder = vaapi` | 用 KMS 抓屏、Intel VAAPI 编码 |
| `adapter_name` | Intel 核显的渲染节点，脚本自动查找，使用按 PCI 地址的固定路径。`renderD` 编号每次开机可能会变，所以不用它 |
| `output_name = HDMI-A-1` | 必须写接口名。写成数字序号的话，连接时会被重新映射到 0 号显示器 |
| `global_prep_cmd` | 连接时执行 `phantomctl session-start`，结束时执行 `session-end`。如果列表里已经有别的软件的命令，会保留它们，只替换本项目自己那一条 |

如果不方便交互输入，可以用环境变量提供账号密码：`SUNSHINE_WEB_CREDENTIALS="用户名:密码" bash install.sh`。核显渲染节点也可以用 `ADAPTER_NAME=/dev/dri/by-path/...` 手动指定。

最后启动 Sunshine：从应用菜单打开，或者执行 `systemctl --user restart app-dev.lizardbyte.app.Sunshine.service`。控制器会跟着它一起启动。如果 Sunshine 本来就在运行，需要重启它才能读到新设置；脚本不会替你重启，因为那样会打断正在进行的串流。

Sunshine 用手动启动还是开机自启（`systemctl --user enable app-dev.lizardbyte.app.Sunshine.service`）都可以。

### 5. 配置 Moonlight

在 iPad 的 Moonlight 里把分辨率设成和 iPad 原生分辨率一致的 **2732×2048**（iPad Air 13″），连接的应用选 **Desktop**。

### 更新

```bash
cd ~/sunshine-ipad-virtual && git pull && bash install.sh
```

重新执行 `install.sh` 就是更新。已经保存的账号密码不会再问。

## 验证

```bash
phantomctl status
# {"lid_closed": false, "session_active": false, "physical_available": true, "phantom_plugged": false, "probe_pending": true, "mode": "primary"}

journalctl --user -u sunshine-phantom -f     # 另开一个终端盯着日志，然后用 iPad 连接
```

连接时预期能看到：

```
helper on: rc=0 status=connected enabled=disabled
applying primary [(0, 0, 2.0, 0, True, [('HDMI-1', '2732x2048@59.999', {})])]
ctl 'session-start' -> ready phantom
```

断开时预期能看到：

```
ctl 'session-end' -> ended
helper off: rc=0 status=disconnected enabled=enabled
restoring [...你原来的屏幕排列...]
```

另外可以在仓库里跑一下单元测试。测试不需要真实的硬件，也不会改动正在运行的系统：

```bash
cd ~/sunshine-ipad-virtual/phantom && /usr/bin/python3 -m unittest discover -p 'test_*.py'
```

## 日常使用

- **启动：** 从应用菜单打开 Sunshine（或执行 `systemctl --user start app-dev.lizardbyte.app.Sunshine.service`），控制器会自动跟着启动。注意菜单里的 "Run in Terminal" 不经过 systemd，这样控制器不会启动：连接时插不上假显示器，Sunshine 会改为抓实体屏。
- **切换模式：** 串流中切换会立即生效。

  ```bash
  sunshine-ipad-mode            # 在 primary / extend 之间切换
  sunshine-ipad-mode extend     # 或直接指定
  ```

- **锁屏和熄屏状态下连接：** 在 iPad 上打开 Moonlight 的主机列表，等一两秒再连接即可，控制器看到来访会先点亮屏幕。锁着屏时，亮起的画面可能是黑的，这是 GNOME 锁屏的遮罩，不影响连接。
- **串流期间：** GNOME 照常熄屏、锁屏，和任何一块显示器一样。在 iPad 上点一下就能唤醒，也可以直接在 iPad 上输入密码解锁。
- **断开时：**
  - 恢复原来的屏幕排列；
  - 如果断开那一刻 iPad 是唯一亮着的屏幕（primary 模式，或者 extend 模式下合着盖、又没有接外接屏），会先锁屏；
  - extend 模式下实体屏一直亮着，所以不锁，和拔掉一块真实显示器一样。
- **电源和合盖：**
  - 串流期间，假显示器算作外接屏，合盖不会休眠，和接着真实外接屏时一样；
  - 断开后十几秒内，logind 会按你的合盖设置处理（用电池时默认休眠）。
- **查看状态：** `phantomctl status` 只读，可以随时执行。**不要在串流时手动执行 `phantomctl session-end`**，它会直接结束会话。

## 卸载

```bash
cd ~/sunshine-ipad-virtual
bash install.sh --uninstall           # 停止并移除控制器，删除安装的文件，从 sunshine.conf 里移除本项目的设置（会先备份）
sudo bash root/install.sh --uninstall  # 拔掉假显示器，删除 helper、sudoers 规则和 EDID
```

卸载只删除 `install.sh` 当初放进去的文件，同一目录里的其他文件不会动。`sunshine.conf` 里的 `capture`、`encoder`、`adapter_name` 是 Sunshine 的普通设置，会保留，不需要的话可以自己删掉。最后重启 Sunshine。

## 适配其他硬件

代码针对 Intel `HDMI-A-1` 和 iPad Air 13″ 写死了几个常量。要改的地方如下：

| 要改的 | 位置 |
|---|---|
| 接口名（默认 `HDMI-A-1`） | `root/sunshine-phantom-drm` 里的 `CONN`；`phantom/controller.py` 里 `phantom_forced()` 的 glob 路径；`sunshine.conf` 里的 `output_name` |
| 分辨率和物理尺寸 | `make-edid.py` 里的时序列表（`cvt_rb2(...)`）和毫米尺寸，然后执行 `python3 make-edid.py edid/<新文件>.bin` 重新生成。如果改了文件名，`root/sunshine-phantom-drm` 和 `root/install.sh` 里的 `FW` 也要一起改 |
| 识别假显示器的 EDID 厂商和名称 | `phantom/display.py` 里的 `PHANTOM_VENDOR` 和 `PHANTOM_PRODUCT`，必须和 `make-edid.py` 生成的保持一致 |
| 假显示器的缩放（默认 2.0） | `phantom/display.py` 里的 `PHANTOM_SCALE` |
| Sunshine 的 systemd 单元名 | `phantom/controller.py` 里的 `SUNSHINE_UNIT`；`systemd/` 下两个文件里的单元名 |

改了 root helper 或 EDID 之后，要重新执行 `sudo bash root/install.sh`；改了其他代码，要重新执行 `bash install.sh`。因为这些文件都是复制过去的。

其他说明：
- 非 Intel 显卡没有测试过，helper 只会选择驱动为 i915 的接口。
- 像素时钟要在显卡的支持范围内：本机的 i915 跑 2732×2048@60（约 355 MHz，超过 340 MHz，需要 HDMI 2.0）没有问题。`make-edid.py` 里的 HDMI Forum VSDB 不能删，否则 i915 会把这个接收端当成上限 165 MHz 的单链 DVI。

## 开发

```
install.sh      安装、更新、卸载用户部分
phantom/        控制器和各模块，以及对应的 test_*.py（test_install.py 测的是 install.sh）
bin/            phantomctl、sunshine-ipad-mode
root/           root helper 和它的安装脚本
systemd/        服务单元、Sunshine 的 drop-in
config/         sunshine.conf 示例
make-edid.py    EDID 生成器（edid/ 下是生成好的文件）
docs/issues/    遇到的上游问题记录
obsolete/       已经放弃的方案（Mutter 虚拟显示器）
```

- **约定：**
  - 只用系统自带的 Python 和 PyGObject，不引入第三方包；
  - 改动按 TDD 进行：先写一个会失败的测试，再改代码；
  - 测试里用桩替换 D-Bus、sudo、systemctl 和 HTTP；`install.sh` 的测试在临时 HOME 里运行。所以测试在任何机器上都能跑，也不会动到正在使用的安装。
- **跑测试：** `cd phantom && /usr/bin/python3 -m unittest discover -p 'test_*.py'`
- **开发循环：** 改完代码，先执行 `bash install.sh` 复制过去，再执行 `systemctl --user restart sunshine-phantom` 生效。如果把 `~/.local/share/sunshine-ipad-virtual` 直接做成指向仓库的符号链接，脚本会识别出来并跳过复制，这样就只需要重启。注意：串流中重启控制器会结束当前会话，这是有意的设计。
- **看日志：**
  - 控制器：`journalctl --user -u sunshine-phantom -f`
  - Sunshine：`journalctl --user -u app-dev.lizardbyte.app.Sunshine -f`
- **调试 Mutter 布局：** `phantom/display.py` 里的 `get_state()` 返回的就是 `GetCurrentState` 的原始数据，可以在 Python 里直接调用来查看。

## 已知限制

- **连接没有建立起来时，假显示器可能会一直插着。** 如果 RTSP 握手失败、抓屏或编码出错，而 Sunshine 又没有打印任何一种"会话结束"的日志，假显示器会一直插着，直到应用被关闭或 Sunshine 退出。这是从源码推断出来的，实际没有遇到过。
- **`Ping Timeout` 这几种结束日志只经过了单元测试和源码分析。** 实测 iPad 断网时，先触发的是 Sunshine 网络库自己的超时，日志里记的是 `CLIENT DISCONNECTED`。
- **控制器重启后会多点亮一次屏幕。** 重启后，控制器会认为 Sunshine 还需要再检测一次编码器，于是下一次检测到来访时会多点亮一次屏幕，没有其他影响。
- **用来读 Sunshine 日志的 journalctl 一旦意外退出，2 秒后才会重启。** 这 2 秒里发生的断开会被漏掉。正常情况下它只有在被杀掉时才会退出。
- **无线鼠标接收器会唤醒合盖的电脑。** 唤醒后，logind 大约 20 秒后会再次休眠。
- **合盖、又没有外接屏时，Mutter 会在盖子后面点亮内屏。** 这是 GNOME 本身的行为。
- **Sunshine 的 terminate 竞态是上游问题。** 本项目只是缩小了触发窗口，详见 `docs/issues/`。

## 许可证

[MIT](LICENSE)
