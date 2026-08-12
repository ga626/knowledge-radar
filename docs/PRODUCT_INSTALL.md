# 产品安装与更新

源码 checkout 的 `scripts\install.bat` 仍是维护者/开发兼容入口。公开包用户使用 `product_install.py`：程序版本与用户数据分离，升级不会覆盖已保存的 API 配置或浏览器资料。

```bat
python scripts\product_install.py plan
python scripts\product_install.py apply --channel stable --archive KnowledgeRadar.zip --receipt candidate-receipt.json
python scripts\product_install.py status
```

默认程序根为 `%LOCALAPPDATA%\KnowledgeRadar\app\<version>`，数据根为 `%LOCALAPPDATA%\KnowledgeRadar\data`。可通过 `--install-root` 和 `--data-root` 指向其他本地可写盘。`plan` 只显示路径和空间估计；`apply` 必须同时提供下载的 ZIP 与匹配回执，核验 SHA-256 和源码身份后才复制公开程序、创建缺失的数据模板、写入单个 Codex MCP block 并部署插件。已有数据文件从不覆盖。

维护者只能为已验证的 `maintainer-main` 制品执行 `apply --channel maintainer-main`；普通用户使用 stable Release。两者任一时刻只会由 `active.json` 指向一个运行版本。若新版本无法工作，`rollback` 恢复先前 active 指针和 MCP block，不删除数据：

```bat
python scripts\product_install.py rollback
```

维护者首次转移旧开发根中的既有本机配置时，先查看不含路径明文的迁移计划，再明确 Apply。迁移只复制、绝不删除或覆盖旧开发根；目标已有不同文件会保留并标记 `NEEDS_REVIEW`：

```bat
python scripts\product_install.py migrate-plan --legacy-root D:\Projects\KnowledgeRadar
python scripts\product_install.py migrate-apply --legacy-root D:\Projects\KnowledgeRadar
```

安装或更新后，由 Codex Desktop 的受支持刷新/重启重新加载 MCP；安装器不会修改 Codex cache 或强制结束 Codex 进程。
