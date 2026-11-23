# 腾讯会议预订平台

一个基于 **Flask + SQLite + pywinauto** 的腾讯会议预订管理系统：用户通过浏览器登录，查看全平台会议、预订/修改/取消腾讯会议；后台 Worker 串行操作已登录的腾讯会议客户端完成真实预订。同一时刻只操作一个腾讯会议任务，避免 GUI 冲突。

---

## 1. 功能总览

| 模块 | 说明 |
| --- | --- |
| 登录/会话 | 用户名+密码登录，PBKDF2-SHA256（31 万轮）存哈希；Bearer Token 会话（默认 7 天）；连续 5 次失败锁定 10 分钟（423） |
| 会议列表 | 所有人可见；默认按**创建时间倒序**（最新在上）；支持按**名称 / 创建者 / 开始时间**排序（升/降）；支持按**起止日期、创建者、主题关键字、状态**筛选 |
| 预订会议 | 输入标题、开始时间、时长（15 分钟起，每 15 分钟一档，最长 4 小时）；**不能跨天**；**不能与已订会议时间冲突**（冲突时返回占用方）；开始时间须晚于当前时间至少 5 分钟 |
| 会议状态机 | `pending`（排队中）→ `booked`（预订成功）/ `failed`（重试耗尽）；删除 → `cancelled`（联动取消腾讯侧）；`failed` 自动重试 2 次，也可手动重试 |
| **展示状态** | `booked` 的会议按当前时间自动显示为 **已预订**（未到时间）/ **会议中**（进行中）/ **已结束**（已过期）；其余显示 预订中 / 预订失败 / 已取消 |
| **状态异常** | 自动标记异常会议：已预订但缺会议号、过了开始时间仍 pending/failed、pending 超 30 分钟未完成；列表淡红底 + ⚠ 原因，可一键筛选 |
| **批量删除** | 仅管理员：勾选多行批量删除（软删除为已取消）或**彻底删除**（purge，从库中移除不可恢复）；已预订的会同步取消腾讯侧；单次上限 200 条；**已取消的会议会被真正移除记录** |
| **删除已取消会议** | 已取消（`cancelled`）的会议可由**管理员**与**创建者本人**删除，用于清理历史记录（此时直接移除行，不可恢复）；已取消会议不可再编辑 |
| 权限 | 普通用户：可改/删**自己且未过期**的会议，不能动他人会议；**已过期会议（开始时间早于当前）仅超级管理员可改/删**；超级管理员：可改/删所有人的会议 |
| 用户管理 | 仅管理员：增加 / 删除 / 修改用户（**用户名、显示名、角色、群组**）、重置密码（可自动生成并返回）；不能删自己、不能删最后一个管理员 |
| 会议统计 | 每个用户查看**自己**的累计时长（仅统计 `booked`）；支持**月 / 季 / 年**三个粒度；**管理员可切换「全平台」统计所有人的会议**，并查看按用户明细 |
| 任务队列 | 仅管理员：查看最近 50 条腾讯侧操作任务（预订/取消）的状态与错误 |
| 会议密码 | 预订时可设置 **6 位数字**入会密码，通过 GUI 自动化真实写入腾讯会议客户端；仅创建者 / 超级管理员 / **同组**成员可见，其他人看到的为空 |
| 复制邀请 | 一键复制邀请文本：主题 / 起止时间（GMT+08）/ 真实入会短链 / 9 位会议号（3-3-3）/ 会议密码；仅创建者 / 超级管理员 / **同组**成员可操作 |
| 群组 | 用户在管理后台可归属一个**群组**（如「销售部」）；同组用户之间可互相查看会议密码、复制对方会议的邀请信息 |

## 2. 角色权限表

| 操作 | 普通用户（本人会议） | 同组用户会议 | 他人（非同组）会议 | 超级管理员 |
| --- | :---: | :---: | :---: | :---: |
| 查看所有会议 | ✅ | ✅ | ✅ | ✅ |
| 预订会议 | ✅ | ✅ | ✅ | ✅ |
| 修改自己的未过期会议 | ✅ | — | — | ✅ |
| 修改他人会议 | ❌ | ❌ | ❌ | ✅ |
| 修改已过期会议 | ❌ | ❌ | ❌ | ✅ |
| 删除自己的未过期会议 | ✅ | — | — | ✅ |
| 删除**自己**的已取消会议 | ✅ | — | — | ✅ |
| 删除他人/已过期会议 | ❌ | ❌ | ❌ | ✅ |
| **批量删除 / 彻底删除会议** | ❌ | ❌ | ❌ | ✅ |
| 手动重试失败会议（自己的/全部） | 仅自己 | — | — | ✅ |
| 查看会议密码 / 复制邀请 | ✅ | ✅ | ❌ | ✅ |
| 查看**自己**的会议统计 | ✅ | — | — | ✅ |
| 查看**全平台**会议统计 | ❌ | ❌ | ❌ | ✅ |
| 用户增删改 / 重置密码 | ❌ | ❌ | ❌ | ✅ |
| 查看任务队列 | ❌ | ❌ | ❌ | ✅ |

## 2.1 会议展示状态（由时间推导，不落库）

`booked` 的会议在列表上按当前时间自动细分为三种**展示状态**：

| 展示状态 | 判定条件 | 说明 |
| --- | --- | --- |
| **已预订** | 当前时间 < 开始时间 | 预订成功，尚未开会 |
| **会议中** | 开始时间 ≤ 当前时间 < 结束时间 | 会议正在进行 |
| **已结束** | 当前时间 ≥ 结束时间 | 会议已过期 |

其余状态：`pending`→**预订中**、`failed`→**预订失败**、`cancelled`→**已取消**。
数据库中的 `status` 仍是 `booked`，展示状态在查询/序列化时按 `start_at`、`end_at` 与当前时间推导，无需定时任务刷新。
列表可按展示状态筛选（已预订 / 会议中 / 已结束 / 预订中 / 预订失败 / 已取消 / **仅状态异常**）。

**状态异常**（供管理员排查）——满足任一条件即在列表中以淡红底 + ⚠ 标注原因：

| 编号 | 异常情形 |
| --- | --- |
| A1 | 已预订（booked）但缺少腾讯会议号（状态与实际不符，无法生成邀请） |
| A2 | 已过开始时间仍为 pending / failed（任务卡死或长期失败） |
| A3 | pending 超过 30 分钟仍未完成（后台队列可能卡住） |

## 2.2 批量删除与彻底删除（仅管理员）

用于清理**状态本身错误、无法通过正常修改/重试修正**的会议：

- **批量删除**（软删除）：勾选多行 → 会议标记为 `cancelled`，记录保留作历史；已预订成功的先排队取消腾讯侧；
- **彻底删除**（purge）：直接从数据库移除记录，**不可恢复**；同样先排队取消腾讯侧（取消任务独立保存会议号，不依赖会议行）；
- 规则：仅超级管理员可用；不存在的 ID 记入 `skipped`；重复 ID 自动去重；单次上限 200 条；
- **已取消的会议**：软删除对它是空操作，所以批量删除时会**直接移除记录**，并在响应 `purged_ids` 中列出。

## 2.3 已取消会议的处理

- 已取消（`cancelled`）的会议**不再显示「编辑」「复制邀请」按钮**，只保留「删除」（后端同样拒绝修改）。
- **管理员**可删除任意已取消会议；**创建者本人**也可删除自己创建的已取消会议。
- 删除已取消会议 = 移除记录（不可恢复），因为软删除对它没有意义；此路径不再排队取消腾讯侧（早已取消过）。

## 3. 预订规则（硬性校验）

1. **时长** 15 分钟起、每 15 分钟一档，最长 240 分钟（4 小时）；
2. **不能跨天**：开始时间 + 时长须在同一自然日 24:00 内；
3. **不能时间冲突**：与任何 `pending`/`booked` 会议区间重叠即拒绝（返回冲突方列表）；
4. **提前量**：开始时间须晚于当前时间至少 `lead_minutes`（默认 5 分钟），给腾讯侧操作留时间；
5. 时间格式 `YYYY-MM-DD HH:MM`，主题 1–80 字符。

修改会议时：若只改主题不校验提前量；若改时间则重新做冲突检查；若原会议已 `booked` 且改了时间 → 排队「先取消旧腾讯会议、再按新时间重订」。

## 4. 架构

```
浏览器(原生 HTML/CSS/JS)
        │  REST /api/*（Bearer Token）
   Flask 应用 server/app.py
     ├─ server/services.py   业务层（校验/权限/状态机）
     ├─ server/database.py   SQLite 数据层（thread-local 连接）
     ├─ server/security.py   密码哈希/Token/登录锁定
     └─ server/worker.py     串行任务 Worker（daemon 线程，1s 轮询）
              │
              ▼
      TMDriver（server/driver/）
        ├─ dryrun.py         模拟驱动（开发自测，默认）
        └─ pywinauto_tm.py   真实驱动（操作腾讯会议客户端 GUI）
```

- **Worker 串行**：全局同一时刻只运行一个腾讯侧 GUI 任务；book 成功 → 会议 `booked` 并记录 9 位会议号；book 失败 → 自动重试（默认 2 次、间隔 60s）后转 `failed`；cancel 失败 → 最多 5 次重试后 `error`；进程重启后残留 `running` 任务自动回队。
- **驱动可切换**：`config.json` 的 `driver_mode` = `dryrun`（本机默认）/ `pywinauto`（生产服务器），一行切换。

## 5. 目录结构

```
tencent-meeting-booking/
├── 需求说明.md              需求梳理与验收清单
├── 设计说明.md              架构/DB/API/流程设计
├── README.md                本文档
├── requirements.txt         flask>=3.0；pywinauto(仅 win32)
├── config.json              运行配置（端口/规则/驱动/选择器）
├── 启动服务.bat             双击启动（自动装依赖）
├── setup_headless.ps1       无显示器虚拟机一键配置（管理员运行）
├── 无显示器虚拟机部署指南.md  无显示器 VM 部署/排障完整说明
├── data/meeting.db          SQLite 数据库（运行时生成）
├── server/
│   ├── app.py               Flask 路由入口（python -m server.app）
│   ├── database.py          DB schema 与数据访问
│   ├── security.py          密码/Token/锁定
│   ├── services.py          业务逻辑与权限
│   ├── worker.py            串行任务 Worker
│   └── driver/
│       ├── base.py          TMDriver 抽象接口
│       ├── dryrun.py        模拟驱动
│       ├── pywinauto_tm.py  腾讯会议 GUI 自动化驱动
│       └── cli.py           驱动命令行工具（inspect/book/cancel/list）
├── web/                     前端（Flask 静态托管）
│   ├── index.html
│   ├── app.js
│   └── style.css
└── scripts/
    ├── init_admin.py            创建/更新超级管理员
    ├── smoke_test.py            全流程冒烟自测
    ├── e2e_user_batch.py        用户编辑/展示状态/批量删除的 HTTP 端到端验证
    ├── preflight_headless.py    无显示器环境部署前自检（只读）
    ├── diag_env.py              环境诊断（窗口/控件树/截屏，排障用）
    └── live_test.py / service_e2e.py / probe_*.py  真实客户端端到端与探针脚本
```

## 6. 安装与部署

### 6.1 开发机（本机，无腾讯会议客户端）

```bat
cd "C:\path\to\tencent-meeting-booking"
python -m pip install -r requirements.txt
python scripts\smoke_test.py      :: 冒烟自测（全部通过 exit 0）
双击 启动服务.bat                 :: 或 python -m server.app
:: 浏览器 http://127.0.0.1:8000
:: 登录 admin / Admin@123456（seed_admin 首次自动创建）
```

> ⚠️ **首次部署务必改掉默认口令**：`config.json` 里的 `seed_admin.password` 只是"首次启动自动建号"的初始值，公开仓库里人人可见。
> 启动后立刻用 `python scripts\init_admin.py --username 你的名字 --password 你的强密码` 重置，或登录后在用户管理里改密码；
> 若要对局域网开放（`host` 改 `0.0.0.0`），务必先改密码并置于反代/防火墙之后。

### 6.2 生产服务器（Windows 11，已安装并登录腾讯会议）

1. 安装 Python 3.10+，把本项目整个目录拷贝过去；
2. 确保腾讯会议客户端**保持登录**（建议用固定 Windows 账号自动登录桌面）；
3. 编辑 `config.json`：
   - `"driver_mode": "pywinauto"`；
   - `"tm.exe"` 指向腾讯会议主程序（默认 `C:\Program Files\Tencent\Meeting\WeMeetApp.exe`，以实际为准）；
   - 若客户端标题/按钮文案与默认选择器不符，先跑 `python -m server.driver.cli inspect` 导出 UIA 控件树，再按需修改 `tm` 段（`main_title_re`、`book_button`、`dialog_title_re`、`ok_button`、`my_meetings_tab`、`delete_menu`、`delete_confirm`、`success_toast_re`）；
4. `python -m server.app` 启动；建议用任务计划程序做成**登录时、交互桌面**自启（不要用 NSSM 等服务方式安装，服务运行在 Session 0 看不到窗口树）；
5. 首次启动后用 `scripts\init_admin.py` 或登录界面设置管理员密码。

> 注意：腾讯会议客户端界面若升级改版，选择器可能需要重新校准（inspect 工具就是为此准备的）。

### 6.3 无显示器虚拟机部署

若服务器是**没有物理显示器的虚拟机**（个人版/专业版账号拿不到官方 API），需按专门指南配置「虚拟显示器 + 自动登录交互桌面 + 开机自启」三件套，详见 **[无显示器虚拟机部署指南.md](无显示器虚拟机部署指南.md)**。简要流程：

```powershell
:: 1) 装 Parsec VDD 并保持一块虚拟显示器在线（缩放 100%）
:: 2) 用专用账户登录一次，装好并保持腾讯会议客户端登录
:: 3) 管理员 PowerShell 一键配置自动登录/禁锁屏/开机自启：
.\setup_headless.ps1 -Username "meet" -Password "你的强密码"
:: 4) 重启后自检（把生成的报告发回排障）：
python scripts\preflight_headless.py
python scripts\diag_env.py
```

> 说明：无显示器方案本质是"给虚拟机造一块假屏幕让 GUI 照常跑"，并非真正的 API 无头；自动登录会把明文密码写入注册表，务必用专用受限账户并隔离部署。若账户将来升级到商业版/企业版并可拿到 AK/SK，可直接新增 `api` driver 切回官方 REST API，其它代码不动。

## 7. 配置说明（config.json）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| host / port | 127.0.0.1 / 8000 | 监听地址 |
| db_path | data/meeting.db | SQLite 文件（相对项目根） |
| session_days | 7 | Token 有效期（天） |
| max_login_fails / lock_minutes | 5 / 10 | 登录失败锁定策略 |
| lead_minutes | 5 | 预订提前量（分钟） |
| max_duration_min | 240 | 单场最长时间（分钟） |
| auto_retry / retry_interval_sec | 2 / 60 | book 失败自动重试次数/间隔 |
| driver_mode | dryrun | `dryrun` / `pywinauto` |
| seed_admin | admin/Admin@123456 | 无管理员时自动创建（**公开仓库可见，部署后立即改**） |
| tm.* | 见文件 | pywinauto 驱动窗口选择器 |

## 8. API 一览（除 /api/login 均需 `Authorization: Bearer <token>`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/login | `{username,password}` → `{token,user}`；失败 401；锁定 423 |
| POST | /api/logout | 注销当前 Token |
| GET | /api/me | 当前用户信息 |
| GET | /api/meetings | 参数 `from,to`(YYYY-MM-DD)、`creator`(用户名或id)、`subject`(关键字)、`display_status`(upcoming\|ongoing\|ended\|pending\|failed\|cancelled)、`anomaly`(1=仅状态异常)、`sort_key`(created_at\|start_at\|subject\|creator)、`sort_dir`(asc\|desc)；默认 `created_at desc`，最多 200 条 → `{items,count}` |
| POST | /api/meetings | `{subject,start_at,duration_min,meeting_password?}` → 201；冲突 409 附 `conflicts`；`meeting_password` 须为 6 位数字，否则 400 |
| PUT | /api/meetings/:id | 修改（同参） |
| DELETE | /api/meetings/:id | 删除（已 booked 会联动取消腾讯侧）；`?purge=1` 彻底删除（仅管理员）；**已取消的会议由本人/管理员删除时直接移除记录** |
| POST | /api/meetings/batch-delete | 批量删除（管理员）`{ids:[],purge?:bool}` → `{deleted,purged_ids,skipped,count,purged,message}`；上限 200 条；已取消的会被真正移除 |
| POST | /api/meetings/:id/retry | 手动重试（仅 failed） |
| POST | /api/meetings/:id/copy-invite | 生成邀请文本 → `{meeting_id,text}`；仅创建者/超管/同组，否则 403 |
| GET | /api/meetings/:id/secret | 查看会议密码 → `{meeting_id,meeting_password}`；仅创建者/超管/同组，否则 403 |
| GET | /api/stats?period=month\|quarter\|year&scope=me\|all | 统计 `{period,scope,items,total_min,count}`；`scope=all` 仅管理员，额外返回 `by_user` 明细 |
| GET/POST | /api/users | 用户列表（支持 `?group=群组名` 筛选）/ 新增 `{username,password?,display_name,role?,group_name?}`（空密码自动生成） |
| PUT | /api/users/:id | 修改 `{username?,display_name?,role?,group_name?}`；字段缺省=不改，`group_name:""`=清空；重名 409 |
| DELETE | /api/users/:id | 删除（会议保留，创建者显示「已删除用户」） |
| GET | /api/groups | 已有群组名 `{items,count}`（管理员；编辑用户时下拉提示） |
| POST | /api/users/:id/reset-password | `{password?}` → 返回生效密码 |
| GET | /api/queue | 最近 50 条腾讯侧任务（管理员） |

错误体统一 `{error: 中文消息, code}`。

## 9. 自测

```bat
python scripts\smoke_test.py          :: 单元/集成级，自带临时库（129 项断言）
python scripts\e2e_user_batch.py      :: 自起 HTTP 服务，验证用户编辑/展示状态/批量删除/统计（63 项断言）
python scripts\e2e_group_password.py  :: 需服务已在 127.0.0.1:8000 运行（28 项断言）
```

`smoke_test.py` 覆盖：登录（错密/成功/锁定 5 次→423/锁定期间正确密码也 423）、用户 CRUD（重名 409/非法名 400/非管理员 403/删自己/删最后管理员）、预订（成功/完全冲突 409/部分重叠 409/紧邻不冲突）、校验（>240 分钟、跨天、提前量、格式）、修改（他人 403、改主题 200、改时间重订 booked、目标冲突 409）、删除（他人 403、管理员 200 cancelled、腾讯侧 cancel 任务 done）、失败自动重试 2 次→failed→手动重试→booked、过期会议（创建者改/删 403、管理员改/删 200）、筛选（主题/创建者/日期）、排序（开始时间升序、创建时间倒序）、统计（月/季/年结构、仅计 booked）、删除用户后会议保留、**群组创建与按群组筛选、会议密码 6 位校验、密码可见性（创建者/同组/管理员可见，非同组脱敏）、copy-invite 与 secret 端点的 200/403 分支、邀请文本含 GMT+08/3-3-3 会议号/入会链接/密码**，以及 v1.1 新增的：
**用户信息编辑（改显示名/用户名/角色/群组、重名 409、非法名 400、群组超长 400、清空群组、未传字段保持不变、新用户名可登录旧名失效、普通用户 403、不能取消自己管理员）、群组列表接口（管理员 200 / 普通用户 403）、展示状态（未到时间=已预订 / 进行中=会议中 / 已过期=已结束 + 三种 display_status 筛选）、状态异常（缺会议号、过期仍 pending、正常会议不误报）、批量删除（2 条、空 400、非整数 400、不存在 skipped、普通用户 403、彻底删除后行消失、单条 purge）**，以及 v1.2 新增的：
**已取消会议删除（管理员删任意 / 创建者删自己的 / 非创建者普通用户 403，删除后记录移除）、批量删除已取消会议（真正移除并计入 purged_ids）、统计 scope（scope=me 无 by_user / scope=all 含 by_user 且总量不少于个人 / 普通用户 scope=all 403 / 非法 scope 400）**。

`e2e_user_batch.py` 自起 werkzeug 服务走真实 HTTP，覆盖静态页面可访问性（首页/app.js/style.css 及新增 DOM 元素）、用户编辑全部分支、群组接口、三种展示状态与筛选、异常识别、批量删除与彻底删除（含腾讯侧 cancel 任务完成校验）、**已取消会议的单条删除权限分支、统计 scope 的 me/all/403/400 分支**，共 63 项断言。

`e2e_group_password.py` 走真实 HTTP，覆盖群组/密码/邀请三功能的端到端链路（28 项断言）。

全部通过打印 `===== 冒烟测试: N/N 通过 =====` / `===== 端到端测试: N/N 通过 =====`，exit 0。

## 10. 已知限制

- 免费版/专业版账号无法使用腾讯会议官方 REST API 创建/取消会议，预订依赖客户端 GUI 自动化，客户端改版时选择器需校准（`inspect` 工具辅助）；商业版/企业版/教育版可通过新增 `api` driver 切回官方无头 API；
- 真实驱动要求腾讯会议客户端已登录且桌面可交互（不能处于锁屏/远程无桌面会话）。锁屏时 `OpenInputDesktop` 返回 0、`GetForegroundWindow` 返回 0，点击无法送达客户端，预订会以 `[dialog] 未找到独立预定对话框…` 失败并写入 `data/driver_failure.log`；无物理显示器的虚拟机须按《无显示器虚拟机部署指南.md》配置虚拟显示器 + 自动登录交互桌面；
- 会议号从腾讯侧的会议号确认窗口 /「我发起的会议」列表提取（9 位），极端情况下可能取不到（不影响预订成功，仅记录提示）；
- 会议密码由 GUI 自动化勾选「入会密码」后填入，客户端改版可能导致定位失败，此时预订会失败并记录 `[password]` 前缀错误；
- 入会短链从客户端窗口文本中用正则 `https?://meeting\.tencent\.com/dm/...` 提取，取不到时邀请文本会省略该行，会议号与密码仍可用；
- 统计仅统计 `booked` 会议；`pending` 不计入，失败/取消不计入。
