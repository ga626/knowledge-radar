# 持续评测使用说明

P0评测账本随源码长期维护。默认只跑确定性用例，不自动登录、收费或刷新宿主。

```powershell
python scripts/kr_evaluate.py validate
python scripts/kr_evaluate.py plan
python scripts/kr_evaluate.py run --affected
python scripts/kr_evaluate.py status
python scripts/kr_evaluate.py history
python scripts/kr_evaluate.py accept-baseline <run-id> --reason "已审阅的本地参照"
python scripts/kr_evaluate.py compare baseline <new-run-id>
python scripts/kr_evaluate.py demo
```

`--root`、`--store`、`--suite`、`--json`放子命令之前。开发根需要嵌入式Python时用其绝对路径；pytest统一由scripts/kr_eval/pytest_entry.py启动，防止误导入另一checkout。

## 新增或修改能力

1. 更新config/evaluation/catalog.json的稳定能力ID、版本、依赖和入口。
2. 在cases.json补业务输入、固定期望、执行层、自动策略和失败复现。每个新增MCP入口均需surface与native业务用例；未实测不填PASS。
3. 成功标准/fixture/判定器变化递增case或oracle版本，写change_reason。旧记录不覆盖。改代码后的plan/status按正文、传递依赖、环境与TTL计算stale。
4. 使用run --affected重测；run --reuse只引用旧run和旧时间。共享/未知变更采用保守失效。
5. compare区分修复、退化与不可比；不自动晋升最新运行为基线。基线接受不等于产品发布批准。

## 三组状态

verdict是当时的PASS/FAIL/ERROR/BLOCKED/SKIPPED/NOT_RUN；applicability是fresh/stale/unknown；readiness是实际账号/平台准备度。登录失效处理fixture可PASS，该账号仍不可用。22个surface通过不等于22条真实业务通过。

结果追加到local/evaluation/runs，包含源码正文/依赖/环境/判定合同与证据hash。损坏或半写run失败关闭。敏感凭据不得进入fixture、Git、公共报告。native MCP、直接SDK、fixture、已安装和用户体验证据不得互相冒充。

## 退役与恢复

删除入口保留retired目录记录。明确删除不再有调用者的代码；存在调用者的兼容层必须登记config/retirement-matrix.json。媒体清理只隔离已登记过期文件，活动lease保护；restore_quarantine验证可信缓存根、SHA且不覆盖新文件。
