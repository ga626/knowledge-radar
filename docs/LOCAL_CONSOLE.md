# 本地控制台

控制台的固定地址是 `http://127.0.0.1:18882/`。它不是外部网站：页面和 API 只监听当前电脑的回环地址，其他设备无法访问；不会显示或上传 API Key、账号、Cookie、任务正文或本机路径。

安装完成后会生成两个等价入口：

- `%LOCALAPPDATA%\KnowledgeRadar\console.cmd`：打开或复用控制台；未运行时启动它。
- `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd`：保留的兼容入口，行为与 `console.cmd` 相同。

`apply` 默认写入当前用户的“启动”目录，在登录 Windows 后无窗口启动本地控制台宿主。因此重启电脑后仍可直接使用固定地址。若不希望登录时常驻，可在安装时加入 `--no-console-autostart`，或随后运行：

```bat
%LOCALAPPDATA%\KnowledgeRadar\console.cmd --disable-autostart
```

恢复常驻：

```bat
%LOCALAPPDATA%\KnowledgeRadar\console.cmd --enable-autostart
```

控制台已经运行时，入口会复用同一实例；若固定端口被非 KnowledgeRadar 程序占用，会停止并说明冲突，绝不悄悄切换到随机端口。更新后使用 `console.cmd --restart` 重启到当前 `active.json` 指定的产品版本。
