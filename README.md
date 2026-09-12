# FsTransfor — GTK4 远程 SSH 文件传输器

基于 GTK4 + libadwaita + paramiko 的多标签双面板文件传输器:

适用于 Linux 图形桌面，使用 [MIT 许可证](LICENSE)。
项目地址：[Luyao-1024/fs_transfor](https://github.com/Luyao-1024/fs_transfor)。

- **多标签页**: 每个标签一个工作区(左右双面板), Ctrl+T 新建 / Ctrl+W 关闭,
  关闭最后一个标签自动补新; 重启恢复所有标签与选中位置
- **连接共享**: 同一服务器(host/port/user)在多个标签页打开时复用同一条
  SSH/SFTP 连接(免重连、免重复认证), 面板和传输任务分别持有引用;
  关闭最后一个远端标签后，排队和运行中的传输继续，任务结束后释放连接;
  一条连接断开, 所有使用它的标签页保留文件界面并显示"连接已断开"提示,
  点工具栏恢复按钮即可重连并回到原目录; 也可主动暂停连接后随时恢复
- **SSH/SFTP**: 密钥 / 密码 / ssh-agent 认证; 首连展示 OpenSSH 风格 `SHA256:` 指纹
  (可与 `ssh-keygen -lf` 逐字比对), known_hosts 校验并在确认后写回(缺失时自动创建,
  权限 0700/0600); 已记录的同类型主机密钥被替换时**只中止连接并列出两侧指纹**,
  不提供“这次相信”, 读写受限会提示但不断连
- **传输**: 面板间拖拽、右键"传输到对侧"、Ctrl+C/X/V 剪贴板(剪切=移动);
  从 Nautilus 等外部文件管理器拖入; 双本地走本地复制, 本地↔远端走网络传输;
  文件先写入目标目录的临时文件、成功后原子提交 —— 取消/失败/断网都不损坏
  已有目标文件; 剪切粘贴只删除已成功移动的源(跳过/失败保留); 目录递归;
  同名弹 覆盖/跳过/取消, 确认等待期间不占用并发额度、排队任务照常推进;
  **单个文件失败只记入该项结果, 同任务其余文件继续**(失败项之后的文件不再被连带放弃);
  符号链接按链接原样重建(悬空链接也不再让整批任务失败, 目标端不支持才退回按内容复制),
  文件权限位(含可执行位)与修改时间尽力还原, 本任务新建的目录保留源端权限;
  同名文件、目录复制进自身、链接别名会被拒绝并说明原因
- **删除**: 本地删除对话框可选 移入回收站(文件管理器可找回) 或 直接删除,
  不支持回收站的位置会询问后永久删除; 远程删除前确认(永久);
  Delete 键 / 右键菜单均可
- **新建与重命名**: 名称只接受单个路径分量, `../x`、`a/b`、`.`、`..` 与控制字符
  会被当场拒绝(不会把文件建到或搬到面板目录之外); 同名新建明确报错,
  重命名到已有名称需二次确认"替换"，默认取消
- **配置记忆**: 保存的服务器一键连接(含复用); `~/.ssh/config` 主机一键填充; 会话自动恢复
- **路径收藏**: 路径栏旁可命名、收藏、跳转或删除常用目录；本地收藏独立保存，
  SSH 收藏按主机、端口和用户名分组，同一服务器跨标签共享
- **高效导航**: 每个面板独立记录前进/后退和最近路径；可快速筛选当前目录，
  Alt+Left/Right 后退/前进、Alt+Up 上一级、Alt+Home 回主目录、Ctrl+L 定位路径栏、Ctrl+F 筛选
- **传输提示**: 每条传输实时速度/进度/取消, 顶栏总速度, Toast 提示
- **任务中心**: 顶栏列表按钮或菜单随时查看全部任务；展开查看源、目标、文件数、
  字节数及逐项结果，支持单项重试、仅重试失败/未完成项、清理已结束记录。
  通知隐藏不删除任务，最近 200 条结果保存到本地，重启后不会自动执行。
- **退出与断开**: 任务中退出可选择等待完成、取消并退出或返回应用；排队任务立即取消。
  连接菜单可断开该服务器的全部面板连接，确认后先取消相关任务、清理临时文件再断开。
- **原生右键菜单**: 在点击位置展开，可超出应用窗口，在屏幕边缘自动避让；
  打开菜单时保留主界面，鼠标单击和键盘激活均可执行操作，分隔线紧凑

## 快捷键

| 键 | 功能 |
|---|---|
| Ctrl+T / Ctrl+W | 新建 / 关闭标签页 |
| Ctrl+C / Ctrl+X / Ctrl+V | 复制 / 剪切 / 粘贴(面板间) |
| Delete | 删除(本地选择回收站或直接删除，远程确认后永久删除) |
| F2 | 重命名 |
| Enter / 双击 | 进入目录 |
| Alt+Left / Alt+Right | 后退 / 前进 |
| Alt+Up / Alt+Home | 上一级 / 主目录 |
| Ctrl+L / Ctrl+F / F5 | 定位路径栏 / 筛选当前目录 / 刷新 |

## 运行

需要 Python 3.10+、GTK4、libadwaita 1.5+ 和 PyGObject。
GTK/PyGObject 由系统包提供，虚拟环境通过 `--system-site-packages` 复用。
例如 Fedora：

```sh
sudo dnf install python3 python3-pip python3-gobject gtk4 libadwaita
```

下载源码并初始化虚拟环境：

```sh
git clone https://github.com/Luyao-1024/fs_transfor.git
cd fs_transfor
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

`paramiko` 安装到虚拟环境。应用需要可用的 Wayland 或 X11 图形会话。

## Fedora RPM 打包

```sh
sudo dnf install rpm-build
./packaging/build-rpm.sh
```

构建产物位于 `~/rpmbuild/RPMS/noarch/`，使用 `sudo dnf install <RPM文件路径>`
安装后，可从应用列表或 `fstransfor` 命令启动。RPM 附带桌面图标、README 和 MIT 许可证。

## 配置与文件操作

默认配置目录为 `~/.config/fs_transfor/`，遵循 `XDG_CONFIG_HOME`；
设置 `FSTRANSFOR_CONFIG_HOME` 可覆盖配置根目录，应用仍在其下创建 `fs_transfor/`。
`servers.json` 保存服务器连接信息，`settings.json` 保存设置和标签会话。
`tasks.json` 保存最近 200 条已结束任务的路径、服务器标识和结果，不包含连接凭据。
密码和密钥口令不写入配置文件；主机密钥通过 `~/.ssh/known_hosts` 校验，
该文件缺失时由应用创建(目录 0700、文件 0600)，用户确认过的密钥才会写回；
主机密钥被替换一律拒绝连接，应用不会自动改写或删除已有条目。

复制/剪切/粘贴使用应用内部剪贴板，尚不与系统文件剪贴板互通。
远端删除和本地“直接删除”不可通过回收站恢复。跨远端复制目前经过本机中转；
文件级原子提交不等于整个目录的事务，部分完成的任务需查看具体提示。
目录内单项失败时该顶层条目按失败处理(源保留)，重试会重扫整个目录并重新确认冲突。
权限与修改时间为“尽力还原”：目标端不支持时只提示不判失败；
符号链接只在两端都支持链接时原样重建，否则退回按内容复制；
属主/属组、ACL、扩展属性和目录的修改时间不在保留范围内。
任务重试会重新扫描、校验源和确认冲突；目录按所选顶层目录作为一项重试，
不会从中断字节继续。已复制的移动项目不自动重复移动或删除源；源删除失败需核对后手动处理。
历史中的 SSH 任务重试前，需先在面板中重新连接对应服务器。

## 测试

```sh
.venv/bin/python tests/selftest.py       # 无 GUI: 传输/取消/覆盖/速度计算逻辑
.venv/bin/python tests/transfer_safety.py # 无 GUI: 自拷贝保护/移动语义/取消与提交安全
.venv/bin/python tests/sftp_contract.py  # 无 GUI: 链接删除/关闭错误传播/严格提交(mock SFTP)
.venv/bin/python tests/connection_lifecycle.py # 无 GUI: 任务连接引用/过期连接请求/操作上下文
.venv/bin/python tests/task_management.py # 无 GUI: 排队取消/部分结果/重试/历史保留与凭据边界
.venv/bin/python tests/hostkey_policy.py # 无 GUI: SHA256 指纹/未知与变化区分/known_hosts 写回
.venv/bin/python tests/transfer_resilience.py # 无 GUI: 逐项隔离/链接与权限保留/确认等待不占并发
.venv/bin/python tests/ui_smoke.py       # 窗口: 面板浏览/导航/会话记忆
.venv/bin/python tests/ui_activate.py    # 窗口: 双击激活/单元格右键
.venv/bin/python tests/ui_widgets.py     # 窗口: 对话框/连接菜单/大文件实时速度
.venv/bin/python tests/ui_integration.py # 窗口: 拖拽处理器模拟/SSH 连接失败路径
.venv/bin/python tests/ui_tabs.py        # 窗口: 标签页/连接共享/剪贴板/回收站/配置迁移
.venv/bin/python tests/ui_delete.py      # 窗口: 删除完整链路(动作→对话框→删除→刷新)
.venv/bin/python tests/ui_delete_perm.py # 窗口: 本地删除对话框(回收站/直接删除/取消)
.venv/bin/python tests/ui_menu.py        # 窗口: 原生菜单定位/跨窗口/屏幕避让/鼠标与键盘激活
.venv/bin/python tests/ui_suspend.py     # 窗口: 连接暂停/恢复/断线保留文件界面
.venv/bin/python tests/ui_bookmarks.py   # 窗口: 本地/SSH 路径收藏分组与持久化
.venv/bin/python tests/ui_navigation.py  # 窗口: 前进后退/最近路径/筛选/导航动作
.venv/bin/python tests/ui_name_guard.py  # 窗口: 新建/重命名名称校验与替换确认全链路
.venv/bin/python tests/ui_conflict_queue.py # 窗口: 覆盖/跳过/取消答复与确认等待不饿死队列
.venv/bin/python tests/ui_task_center.py # 窗口: 全部任务/通知与历史分离/失败重试/记录清理
.venv/bin/python tests/ui_shutdown.py    # 窗口: 返回应用/取消退出/等待退出/主动断开
```

测试通过 `FSTRANSFOR_CONFIG_HOME` 环境变量隔离配置目录, 不会读写真实会话;
并使用独立的 Application ID, 不会与正在运行的实例互相干扰。
UI 测试会短暂打开窗口，需要图形会话；多个脚本使用相同测试应用 ID，应顺序运行：

```sh
for test in tests/ui_*.py; do
    .venv/bin/python "$test" || exit 1
done
```

SFTP 合约与主机密钥测试使用模拟后端和临时 HOME，不触碰真实 `~/.ssh`，
也不能替代真实服务器上的认证、网络中断和原子覆盖验证。

## 后续开发

详见 [项目检视与后续完善计划](docs/PROJECT_IMPROVEMENT_PLAN.md)：包含已发现问题、
修复优先级、技术方案、阶段需求、验收标准和回归测试清单。文档中的待办尚未代表已实现。

## 许可证

Copyright (c) 2026 Luyao-1024。项目采用 [MIT License](LICENSE)，允许使用、修改和分发，
分发时须保留版权声明和许可证。第三方依赖遵循各自许可证。
