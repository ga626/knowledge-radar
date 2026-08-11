# XHS Bridge

状态：diagnostic_candidate。

此目录保存小红书桥接脚本和 Node 依赖。默认只用于诊断或显式候选验证，不进入生产主链。

边界：

- 生产默认关闭：`KR_XHS_BRIDGE_PRODUCTION_ENABLED=0`。
- 只有通过 acceptance gate 后才允许单次显式验证。
- 不允许作为小红书 search/detail/main_chain 的默认 fallback。
- CJS 文件是当前本仓库 bridge 事实源；MJS 文件保留为历史参考。
