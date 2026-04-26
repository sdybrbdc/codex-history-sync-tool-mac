# Codex History Sync Tool

一个用于恢复 Codex Desktop 本地历史对话显示的小工具。

## 说明

这个版本基于公开项目 `GODGOD126/codex-history-sync-tool` 做了本地整理，并补充了可运行的 macOS 版本入口与 `.app` bundle。

当你切换 API、provider 或登录方式之后，Codex Desktop 有时会出现“本地历史明明还在，但侧边栏看不到”的情况。这个工具会检查本机的本地历史数据库，并把旧线程重新挂到当前正在使用的 `model_provider` 下面。

现在支持两种同步方式：

- 全量同步：把所有旧 provider 下的线程统一归到当前 provider
- 定向同步：先按项目分组，再只同步你选中的会话

macOS 版本还会同步每个 `rollout-*.jsonl` 第一行的 `session_meta.model_provider`。这是为了避免只改数据库后，Codex Desktop 刷新或重建索引时又从 JSONL 元数据把旧 provider 读回来。

## 这个工具能做什么

- 查看当前本机 Codex 历史线程属于哪些 provider
- 按项目分组查看线程
- 查看某个项目下的具体会话
- 只把选中的会话同步到当前 provider
- 一键把旧 provider 下的线程同步到当前 provider
- 同步 macOS 本地 rollout 元数据，避免刷新后回退到旧 provider
- 在同步前自动备份数据库和 rollout 元数据
- 从备份恢复数据库和 rollout 元数据
- 提供一个可直接点击的 Windows 图形界面

## 适用场景

- 你切换了不同 API
- 你切换了不同 provider
- 你切换了登录方式
- 你确认本地历史文件还在，但 Codex Desktop 左侧历史列表变空了

## 不适用的场景

- 云端账号之间的聊天记录互相同步
- 本地历史文件已经被删除
- 不同电脑之间迁移聊天记录

## 运行环境

### Windows

- Windows
- PowerShell 5.1 或更高版本
- 已安装 Python，并可通过 `py -3` 调用
- 本机存在 Codex Desktop 本地数据目录，通常是 `%USERPROFILE%\\.codex`

### macOS

- macOS
- 已安装 Python 3，并可通过 `python3` 调用
- 默认使用 `~/\.codex` 作为 Codex 本地数据目录
- 内置 `tkinter` 可用时，可直接启动图形界面
- 兼容新版 `config.toml` 缺少顶层 `model_provider` 的情况，会根据当前 model 的最近线程推断 provider

## 快速使用

### 图形界面

#### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1
```

#### macOS

```bash
python3 ./launch_ui_mac.py
```

或者双击：

```bash
./launch_ui_mac.command
```

也可以直接双击：

- `Codex History Sync Tool.app`

### 创建桌面快捷方式

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1 -InstallShortcutOnly
```

### 查看当前状态

```powershell
py -3 .\sync_backend.py --json status
```

macOS:

```bash
python3 ./sync_backend.py --json status
```

### 查看项目分组

```powershell
py -3 .\sync_backend.py --json projects
```

macOS:

```bash
python3 ./sync_backend.py --json projects
```

### 查看某个项目下的会话

```powershell
py -3 .\sync_backend.py --json threads --group-key repo:github.com/example/repo
```

macOS:

```bash
python3 ./sync_backend.py --json threads --group-key repo:github.com/example/repo
```

### 只同步选中的会话

```powershell
py -3 .\sync_backend.py --json sync-selected --thread-id <thread-id-1> --thread-id <thread-id-2>
```

macOS:

```bash
python3 ./sync_backend.py --json sync-selected --thread-id <thread-id-1> --thread-id <thread-id-2>
```

### 执行同步

```powershell
py -3 .\sync_backend.py --json sync
```

macOS:

```bash
python3 ./sync_backend.py --json sync
```

### 手动创建备份

```powershell
py -3 .\sync_backend.py --json backup
```

macOS:

```bash
python3 ./sync_backend.py --json backup
```

### 从最新备份恢复

```powershell
py -3 .\sync_backend.py --json restore
```

macOS:

```bash
python3 ./sync_backend.py --json restore
```

## 备份说明

- 每次同步前都会自动创建一份数据库备份
- macOS 版本同步时还会创建一份 `session_meta.*.jsonl` 备份，用于恢复 rollout 第一行元数据
- 每次恢复前也会先创建一份安全备份
- 备份默认保存在 `%USERPROFILE%\\.codex\\history_sync_backups`

## 使用建议

- 最稳妥的做法是先关闭 Codex Desktop，再执行同步或恢复
- 如果同步完成后历史列表没有立刻刷新，重开一次 Codex Desktop 即可
- “按项目分组”默认优先按 git 仓库归并；识别不到仓库时才按 `cwd` 单独分组
- 定向同步只会修改你选中的线程，不会改其他项目或其他会话

## 项目文件

- `sync_backend.py`：后端同步、备份、恢复逻辑
- `launch_ui.ps1`：Windows 图形界面
- `launch_ui_mac.py`：macOS 图形界面
- `launch_ui_mac.command`：macOS 双击启动入口
- `Codex History Sync Tool.app`：macOS app bundle

## 免责声明

这个工具直接操作本机 Codex 的本地状态数据库。虽然已经做了自动备份，但仍建议你在使用前先理解它的作用，并自行确认本地数据目录状态。
