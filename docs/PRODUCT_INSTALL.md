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

首次填写配置、查看能力包状态或清理过期媒体缓存，请运行安装器生成的 `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd`，并阅读 [首次成功](FIRST_SUCCESS.md) 与 [能力包与本地健康台](CAPABILITY_PACKS.md)。不要从解压目录或 app 目录运行 `scripts\setup_wizard.bat`；它是源码 checkout 的兼容入口。
