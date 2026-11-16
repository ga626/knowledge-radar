# 官方搜索结果与可见页面交回

仅在需要将宿主搜索/打开结果保存进 KR 时读；官方搜索可以直接开始。

用 `record_research_candidates_tool` 一次性交回真实 `host_reference`、实际 `query` 和工具名；无可用载荷用 `unavailable`，确认零命中才用 `no_results`。宿主声明不是 KR 原生取证，不编造引用或工具返回。

已打开页面的实际可见文字可附 `visible_text`、`visible_scope`（`excerpt`/`page_text`）、`truncated` 与带时区 `retrieved_at`，保存为可回读快照；摘要和截断内容不称全文。

保留实际 `observed_url`、`observed_reference`；请求特定页时另附 `requested_url`。`url` 对应实际页面，重定向仅附实际观察到的 `redirect_chain`。URL/ref 错配停止挂接，身份缺失标未核验，不把错页正文挂到目标来源。KR 不能替宿主证明引用真实性；精确字段组合看工具合同。
