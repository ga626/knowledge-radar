# 产品安装与更新

源码 checkout 的 `scripts\install.bat` 仍是维护者/开发兼容入口。公开包用户使用 `product_install.py`：程序版本与用户数据分离，升级不会覆盖已保存的 API 配置或浏览器资料。

```bat
python scripts\product_install.py plan
python scripts\product_install.py apply --channel stable --archive KnowledgeRadar.zip --receipt candidate-receipt.json
python scripts\product_install.py status
```

默认程序根为 `%LOCALAPPDATA%\KnowledgeRadar\app\<version>`，独立 Python 运行时为 `%LOCALAPPDATA%\KnowledgeRadar\runtime\<version>`，数据根为 `%LOCALAPPDATA%\KnowledgeRadar\data`。可通过 `--install-root` 和 `--data-root` 指向其他本地可写盘。`apply` 需要本机可用的 Python 3.12：先核验 ZIP/回执、复制公开程序、创建版本化运行时并安装依赖、启动预检；全部成功后才创建缺失的数据模板、写入单个 Codex MCP block 并部署插件。运行时准备失败不会切换已有 active 或覆盖用户数据。

维护者只能为已验证的 `maintainer-main` 制品执行 `apply --channel maintainer-main`；普通用户使用 stable Release。两者任一时刻只会由 `active.json` 指向一个运行版本。若新版本无法工作，`rollback` 恢复先前 active 指针和 MCP block，不删除数据：

```bat
python scripts\product_install.py rollback
```

维护者首次转移旧开发根中的既有本机配置时，先查看不含路径明文的迁移计划，再明确 Apply。迁移只复制、绝不删除或覆盖旧开发根；目标已有不同文件会保留并标记 `NEEDS_REVIEW`：

```bat
python scripts\product_install.py migrate-plan --legacy-root D:\Projects\KnowledgeRadar
python scripts\product_install.py migrate-apply --legacy-root D:\Projects\KnowledgeRadar
```

## 将已安装的数据根迁到其他盘

当产品数据在系统盘占用较大空间时，先关闭 KnowledgeRadar 管理的浏览器，再用**当前已安装版本**中的安装器生成只读计划。目标必须是尚不存在或为空的目录；安装器会复制、逐文件 SHA-256 核验、切换唯一 MCP 配置，然后保留旧数据根供回滚。它不会自动执行，也不会删除旧根、Profile、Cookie、密钥、模型或未知文件。

```bat
python scripts\product_install.py data-move-plan --data-root D:\Software\KnowledgeRadarData
python scripts\product_install.py data-move-apply --data-root D:\Software\KnowledgeRadarData --confirmation <plan 中的 confirmation_token>
python scripts\product_install.py data-move-rollback
```

计划若发现 `Singleton*` 浏览器锁、空间不足、目标非空或数据在计划后发生变化，会拒绝执行；重新关闭浏览器并生成新计划即可。迁移完成后按 Codex 的受支持方式刷新或重启 MCP，再调用 `health_check` 和 `get_capabilities`。媒体缓存可在本地健康台隔离；其它分类只允许用户手动处理，分类规则见 [隐私和本地状态](PRIVACY_AND_LOCAL_STATE.md)。

安装或更新后，由 Codex Desktop 的受支持刷新/重启重新加载 MCP；安装器不会修改 Codex cache 或强制结束 Codex 进程。

首次填写配置、查看能力包状态或清理过期媒体缓存，可直接打开 `http://localhost:18882/`。Windows 同一安装用户无需浏览器票据或 Cookie；安装器生成的 `%LOCALAPPDATA%\KnowledgeRadar\console.cmd`（`configure.cmd` 为兼容别名）保留为手动恢复入口。阅读[本地控制台](LOCAL_CONSOLE.md)、[首次成功](FIRST_SUCCESS.md) 与 [能力包与本地健康台](CAPABILITY_PACKS.md)。安装默认注册当前用户登录启动及同任务补启动，可用 `--no-console-autostart` 禁用。开发端口 `18883` 仅维护者显式启用，不随普通安装创建。不要从解压目录或 app 目录运行 `scripts\setup_wizard.bat`；它是源码 checkout 的兼容入口。

## 可选浏览器与 bridge 依赖

首次安装不会下载 Playwright 浏览器或 Node.js bridge 依赖。需要时可在本地控制台先看计划、再第二次确认；也可手动执行以下命令。`plan` 不写入数据，`apply` 只接受当前计划产生的确认令牌。浏览器下载不登录、不调用付费 API；小红书 bridge 依赖不会绕过验证码或自动启用生产兜底，完成后按 Codex 支持方式刷新或重启。

```bat
python scripts\product_install.py capability-plan --capability browser
python scripts\product_install.py capability-apply --capability browser --confirmation <plan 中的 confirmation_token>

python scripts\product_install.py capability-plan --capability xhs_bridge
python scripts\product_install.py capability-apply --capability xhs_bridge --confirmation <plan 中的 confirmation_token>
```

两项内容都保存在当前数据根，更新程序不会覆盖。若 Node.js/npm 不可用，bridge 安装会停止并提示先安装它们；安装器不会替你更改系统级 Node、浏览器或代理设置。

## 已选组件随更新保留

更新前已安装并登记的媒体下载器、转写运行时与 Playwright Chromium，会在新版本运行时中恢复后再切换安装身份。未选择的组件和模型不会自动下载；缓存与账号资料留在数据根。FFmpeg 统一按显式配置、数据根工具目录与系统安装解析。组件准备失败时保留已有 active。


## 离线锁定与候选隔离验证

核心运行时只使用config/runtime-win-py312.lock与runtime-artifacts.json列出的同一wheelhouse，逐SHA核验后通过--no-index --require-hashes安装。当前受控目标为Windows x64/Python3.12；其他平台应生成并验收对应锁，不可复用本锁冒充通用安装。

verify-install需指定独立install/data根和同一ZIP/receipt；只stage程序和运行时，不写active.json、不注册Codex或自启动。待正式收口授权和main身份满足后才可apply。已有同版本但内容不同会拒绝覆盖，应使用独立根或正式新版本。程序、锁与ZIP不一致直接失败，不回退在线解析。回退先验证旧程序和运行时，再切active与插件。

```powershell
python scripts/product_install.py verify-install --package-root <extracted-package> --install-root <isolated-root> --data-root <isolated-data> --archive <frozen-zip> --receipt <candidate-receipt>
```

浏览器/模型/下载器为可选资产；核心依赖安装成功不等于账号、浏览器、模型已就绪。其实际身份须在能力与候选回执中读取。

维护者可在自己的安装执行 `python scripts/product_install.py developer-view --install-root <install-root> --developer-view enable`，为该安装的数据根写入本地维护者标记；disable关闭。下一次加载页面生效，不改MCP、账号或active身份。普通用户默认没有开发者界面，服务端同时限制评测明细和原始研究账本下发。

研究成果在同一任务中选择固定证据和报告后可下载ZIP；Obsidian是可选目标，只显式写入用户指定Vault的KnowledgeRadar Inbox。同一内容重复导出复用同一目录，已被用户修改的内容不会覆盖。没有Obsidian/MensLink时仍能保存、回读和下载成果。
