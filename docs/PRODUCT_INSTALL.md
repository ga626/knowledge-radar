# 产品安装与更新

源码 checkout 的 `scripts\install.bat` 仍是维护者/开发兼容入口。公开包用户使用 `product_install.py`：程序版本与用户数据分离，升级不会覆盖已保存的 API 配置或浏览器资料。

Browser Link 与 KR OpenCLI Bridge 的扩展文件随主程序分发，不走浏览器商城。安装器按清单验证全部文件、版本与固定扩展 ID，将文件准备到安装根的 `components/extensions/<组件>/loaded`；此路径在升级后保持稳定。首次仍需本人在 Chrome 开启开发者模式，选择“加载已解压的扩展”并确认权限。控制台提供准备、路径复制和逐步引导；后续更新跟随 KR 安装包，用户在 Chrome 重新加载扩展。公钥固定 ID，不冒充商城签名或审批。

Browser Link 的 Native Host 仅注册 HKCU 的 `com.knowledgeradar.browser_link`，使用 Windows GUI 子系统可执行文件，通过标准流通信而不依赖控制台窗口。升级前排空任务；注册属于其他安装时保留其文件和注册，由组件页“检查Host交接 → 确认接管Host”按内容绑定的计划接管。人改扩展文件、注册变化或协议冲突均保留并报告，不静默覆盖。浏览器断线只恢复通道，不重放未知结果的网站操作，也不关闭日常 Chrome。

```bat
python scripts\product_install.py plan --choose-data-root
python scripts\product_install.py apply --choose-data-root --channel stable --archive KnowledgeRadar.zip --receipt candidate-receipt.json
python scripts\product_install.py status
```

默认程序根为 `%LOCALAPPDATA%\KnowledgeRadar\app\<installation_id>`，独立 Python 运行时为 `%LOCALAPPDATA%\KnowledgeRadar\runtime\<installation_id>`。首次安装必须用 `--choose-data-root` 打开 Windows 目录选择器，或显式传 `--data-root <你选择的目录>`；取消选择不会开始安装。程序根可用 `--install-root` 修改。升级时省略数据参数，直接继承 `active.json` 的既有数据根；已有路径不可用时停止，不回落到默认目录。升级不能借 `--data-root` 换根，改位置须走下面的迁移流程。

配置、任务、归档授权、组件准备回执和浏览器绑定均在所选数据根；外部 Obsidian 库保持原位置。`active.json` 是安装根中的单一定位指针，程序文件与用户状态隔离。`apply` 需要本机可用的 Python 3.12：先核验 ZIP/回执、复制程序、准备版本化运行时并预检；成功后创建缺失模板、维护唯一 Codex MCP block。运行时准备失败不会激活新版本。

main 的 CI 实验包仅交付到已核实仅本人可访问的私有仓库，公开 Actions 不上传实验安装包；缺配置或权限无法核实时停止交付。只有用户针对拟发布制品明确说“可以公开发布”才可提升同一包为 stable，普通用户此前继续获得上一正式版。维护者只能为已验证的 `maintainer-main` 制品执行 `apply --channel maintainer-main`；普通用户使用 stable Release。两者任一时刻只会由 `active.json` 指向一个运行版本。若新版本无法工作，`rollback` 恢复先前 active 指针和 MCP block，不删除数据：

```bat
python scripts\product_install.py rollback
```

维护者首次转移旧开发根中的既有本机配置时，先查看不含路径明文的迁移计划，再明确 Apply。迁移只复制、绝不删除或覆盖旧开发根；目标已有不同文件会保留并标记 `NEEDS_REVIEW`：

```bat
python scripts\product_install.py migrate-plan --legacy-root <旧目录> --data-root <已选择的数据目录>
python scripts\product_install.py migrate-apply --legacy-root <旧目录> --data-root <已选择的数据目录>
```

## 将已安装的数据根迁到其他盘

当产品数据在系统盘占用较大空间时，先关闭 KnowledgeRadar 管理的浏览器，再用**当前已安装版本**中的安装器生成只读计划。目标必须是尚不存在或为空的目录；安装器会复制、逐文件 SHA-256 核验、切换唯一 MCP 配置，然后保留旧数据根供回滚。它不会自动执行，也不会删除旧根、Profile、Cookie、密钥、模型或未知文件。

```bat
python scripts\product_install.py data-move-plan --data-root <新的空数据目录>
python scripts\product_install.py data-move-apply --data-root <新的空数据目录> --confirmation <plan 中的 confirmation_token>
python scripts\product_install.py data-move-rollback
```

控制台“设置与帮助”提供同一计划、确认迁移及两步确认恢复入口。迁移 journal 在复制前写入，期间暂停新写入；复制中断可继续同一计划，路径重绑定中断须先恢复保留的原目录。目标文件始终保留；迁移后新增数据会阻止直接回退，要求先核对两端。`Singleton*` 锁、活动任务/租约、空间不足、目标非空或计划后数据变化都会阻止不安全切换。迁移完成后重新打开控制台，按 Codex 的受支持方式刷新 MCP，再核验 `health_check` 与 `get_capabilities`。

## Obsidian 全文归档

Obsidian 是选装软件，组件页提供官方下载入口；安装软件与授权资料库分别检测。由 Codex 显式绑定项目和获准库，任务按 `KR研究/<动态项目名>/<年份>年/<月>月<日>日 <时>：<分> <任务名>/` 保存，Windows 时间分隔符用全角冒号。最终报告统一为 `总报告.md`，阶段报告与重名成果保留可读中文名称。

全部有效正文/可读转译增量归档，包含未引用、低相关及反证；无法转译时明确报缺口。报告交付与来源交付分别记录，报告到达不代表资料齐全。晚绑定授权会补交已固定的历史版本，不重新抓取替代旧证据。旧布局只通过 `knowledge_delivery` 的 `plan_archive_layout → apply_archive_layout` 迁移，同步导航和数据库；不手搬、人改不覆盖、删除不复活。

安装或更新后，由 Codex Desktop 的受支持刷新/重启重新加载 MCP；安装器不会修改 Codex cache 或强制结束 Codex 进程。

执行内核只在显式启动时创建；状态查询不自动启动或恢复任务。Windows启动会脱离调用命令所属的Job，避免命令/宿主退出连带回收owner；系统拒绝脱离时明确报错，可从本人终端显式启动，不静默退回短命子进程。正常启动/退出在现有startup.log留PID和代际。未知结果仍按原request_id查回执，不因内核重启而重发。

首次填写配置、查看能力包状态或清理过期媒体缓存，可直接打开 `http://localhost:18882/`。Windows 同一安装用户无需浏览器票据或 Cookie；安装器生成的 `%LOCALAPPDATA%\KnowledgeRadar\console.cmd`（`configure.cmd` 为兼容别名）保留为手动恢复入口。阅读[本地控制台](LOCAL_CONSOLE.md)、[首次成功](FIRST_SUCCESS.md) 与 [能力包与本地健康台](CAPABILITY_PACKS.md)。安装默认注册当前用户登录启动及同任务补启动，可用 `--no-console-autostart` 禁用。开发端口 `18883` 仅维护者显式启用，不随普通安装创建。不要从解压目录或 app 目录运行 `scripts\setup_wizard.bat`；它是源码 checkout 的兼容入口。

## 可选浏览器与小红书连接

首次安装不会下载 Playwright 浏览器或 OpenCLI 依赖。Playwright 可在本地控制台先看计划、再第二次确认，也可手动执行以下命令。`plan` 不写入数据，`apply` 只接受当前计划产生的确认令牌。浏览器下载不登录、不调用付费 API。

```bat
python scripts\product_install.py capability-plan --capability browser
python scripts\product_install.py capability-apply --capability browser --confirmation <plan 中的 confirmation_token>
```

小红书唯一执行方式是日常 Chrome＋OpenCLI。旧 `xhs_bridge` 安装操作已拒绝执行，不会安装 Node Bridge。当前维护者使用 `scripts/kr_opencli.py prepare --source-root <已审查的OpenCLI构建>` 将固定依赖准备到本运行目录；`start` 启动 daemon，`inventory` 列出在线连接，`bind --browser chrome --context-id <核对的context>` 绑定本人确认的日常 Chrome。KR OpenCLI Bridge 扩展随包提供，由用户在 Chrome 开发者模式加载并确认权限后启用，账号仍在 Chrome 登录，不迁移 Cookie。运行时准备暂不自动获取任意新构建，扩展文件和运行时状态分别检测；详见[能力包](CAPABILITY_PACKS.md)。

## 已选组件随更新保留

KnowledgeRadar Browser Link（KR 浏览器连接器）是招聘浏览器通道的标配组件，知乎、BOSS、猎聘动态页、脉脉共用一个 Native Host；小红书使用 KR OpenCLI Bridge；V2EX 与可读公开页面继续走 HTTP。Host 源码随产品交付，安装器在用户级 HKCU 注册版本化可执行文件，更新先停止接收、等待已有任务结束，再切换注册；失败恢复注册和设置。仅回收 KR 的工作标签，不关闭日常 Chrome，不复制账号。

个人 Chrome 使用开发者模式加载随包扩展，首次需本人确认，更新后在扩展管理页重新加载。安装器保持加载路径和 ID，校验内容清单；文件准备完成不等于扩展已连接，连接也不等于站点登录。未知注册通过明确 Host 交接计划处理。

更新前已安装并登记的媒体下载器、转写运行时与 Playwright Chromium，会在新版本运行时中恢复后再切换安装身份。未选择的组件和模型不会自动下载；缓存与账号资料留在数据根。FFmpeg 统一按显式配置、数据根工具目录与系统安装解析。组件准备失败时保留已有 active。


## 离线锁定与候选隔离验证

安装器同时生成 `knowledgeradar.cmd`；每次执行按 `active.json` 选择当前程序、运行时与数据根，和 MCP 共用同一个执行内核。`capabilities --tool read_knowledge` 按需展开合同；`runtime status`、`runtime receipts` 和 `runtime receipt <request_id>` 只读诊断，`runtime start` 显式启动。MCP 会话连接时自动连接或启动内核；会话断开不会取消任务。固定证据批次用 `knowledge batch --input <JSON> --output <新文件>`，版本对比用 `knowledge compare <左版本> <右版本> --output <新文件>`；不覆盖已有文件。升级、回滚或数据迁移先检查内核，忙时保留工作并拒绝切换，空闲时安全退出再交接。上述为新版候选合同，须经同制品安装验收后才作为已安装能力。

核心运行时只使用config/runtime-win-py312.lock与runtime-artifacts.json列出的同一wheelhouse，逐SHA核验后通过--no-index --require-hashes安装。当前受控目标为Windows x64/Python3.12；其他平台应生成并验收对应锁，不可复用本锁冒充通用安装。

verify-install需指定独立install/data根和同一ZIP/receipt；只stage程序和运行时，不写active.json、不注册Codex或自启动。待正式收口授权和main身份满足后才可apply。installation_id 由版本和制品元数据指纹组成；相同ID不同内容拒绝覆盖，同版本不同制品各用独立目录并支持回滚。程序、锁与ZIP不一致直接失败，不回退在线解析。回退先验证旧程序和运行时，再切active与插件。

```powershell
python scripts/product_install.py verify-install --package-root <extracted-package> --install-root <isolated-root> --data-root <isolated-data> --archive <frozen-zip> --receipt <candidate-receipt>
```

浏览器/模型/下载器为可选资产；核心依赖安装成功不等于账号、浏览器、模型已就绪。其实际身份须在能力与候选回执中读取。

维护者可在自己的安装执行 `python scripts/product_install.py developer-view --install-root <install-root> --developer-view enable`，为该安装的数据根写入本地维护者标记；disable关闭。下一次加载页面生效，不改MCP、账号或active身份。普通用户默认没有开发者界面，服务端同时限制评测明细和原始研究账本下发。

研究成果在同一任务中选择固定证据和报告后可下载ZIP；Obsidian是可选的文本交付目标。Codex显式指定项目身份、本地报告目标和获准Vault，KR按动态项目/年份/日期时间任务归档全部有效转译正文与报告，不按自身cwd猜归属。重试沿固定版本复用已分配位置，用户修改、移动或删除的目标不会被自动覆盖或重建。没有Obsidian/MensLink时仍能保存、回读和下载成果。


### 浏览器组件与来源说明

保留官方 OpenCLI 和 KR Browser Link 各自负责的通道，不要求安装修改版 OpenCLI。连接恢复后无需反复手工重试；若扩展身份或Native Host配对有误，按诊断修复，不以重装、搬Cookie或重复登录代替定位。默认在日常Chrome的独立工作窗口后台执行，仅确认需要人工交互或存在可见性阻断时才前置。

当前来源、内容形态、学术独立全文任务及暂停边界见[来源能力指南](CAPABILITIES.md)。注册或配置成功不代表账号、所有内容形态及媒体都已真实验收。
