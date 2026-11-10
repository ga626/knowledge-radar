# 来源能力指南（自动生成）

由 scripts/generate_tool_manual.py 从同一生态声明和工具合同生成；禁止手改。
目录仅帮助Agent选择来源；注册、启用、账号就绪和真实验收分开，来源数量不决定证据饱和。
工具合同指纹：`1eb480e159ccb75fa75c6268bce8dbc378be19810ce5b5800d93ffa10dd2c568`

## 微信公众号公开文章生态

生态：`wechat_public_article_ecology`

公开索引可发现候选文章 URL、标题、摘要和来源线索；正文抽取成功后才可升级证据强度。

候选工具：`search_wechat_articles`、`get_content_detail`、`kr_web_search`、`extract_web_page`、`extract_dynamic_page`

内容：公众号文章、机构推送、作者专栏、行业评论、自媒体长文、活动纪要

边界：open web index coverage is uneven；article body extraction can fail；publication time and account identity may need verification；读者留言已暂停；正文与媒体保持独立能力。

## B站视频与弹幕评论生态

生态：`bilibili_video_ecology`

公开搜索可返回视频标题、URL、UP 主、简介和互动数据；详情、字幕、评论和多模态需按预算进入。

候选工具：`search_bilibili`、`get_content_detail`、`get_task_status`

内容：视频、字幕/转写、评论、互动数据、UP主频道内容

边界：转写和评论可能产生后台任务；媒体分析成本较高；互动数据不等于代表性民意

## YouTube 公开视频生态

生态：`youtube_video_ecology`

公开视频搜索可返回标题、URL、频道、摘要和发布时间线索；字幕、评论和媒体分析需详情链路验证。

候选工具：`search_youtube`、`get_content_detail`、`get_task_status`

内容：公开视频、频道元数据、字幕/转写、评论

边界：API key and network reachability affect availability；会员/私密/地区受限内容不可访问

## 知乎问答与专栏讨论生态

生态：`zhihu_discussion_ecology`

公开搜索可返回问题、回答、文章、作者和赞同线索；事实采信需要详情和跨源验证。

候选工具：`search_zhihu`、`get_content_detail`

内容：问题、回答、文章、作者信息、赞同/评论线索

边界：登录态和反爬健康影响详情；高赞不等于事实正确

## 小红书生活经验与图文视频生态

生态：`xiaohongshu_experience_ecology`

公开/登录态可用时搜索图文或视频笔记候选；稳定事实声明需要标注样本偏差和访问边界。

候选工具：`search_xiaohongshu`、`get_content_detail`

内容：图文笔记、短视频、评论、图片 OCR 线索

边界：登录/冷却/反爬边界明显；样本偏差和营销内容需要标注

## GitHub 开源项目生态

生态：`github_repository_ecology`

仓库发现保留原查询与上游顺序；详情按需读取固定提交的 README、源码、目录及分页 Issue/PR。stars 不证明实现。

候选工具：`search_github_repositories`、`get_content_detail`、`kr_web_search`

内容：仓库、固定版本 README/源码、Issue/PR多流讨论、语言、stars、更新时间

边界：搜索受 GitHub 可访问性和速率限制影响；stars 不等于质量

## 开放学术文献与元数据生态

生态：`academic_literature_ecology`

公开元数据可返回题名、作者、年份、DOI、摘要和开放全文线索；全文证据强于元数据。

候选工具：`search_academic`、`get_content_detail`、`get_task_status`、`read_knowledge`、`extract_web_page`

内容：论文元数据、DOI、摘要、开放全文线索、引用格式、独立全文任务、页级PDF/OCR证据

边界：不绕过登录、验证码、付费墙或机构授权；不同 provider 覆盖范围不同

步骤：discovery

```kr-tool-example
{
  "tool": "search_academic",
  "arguments": {
    "query": "研究问题"
  }
}
```
绑定：{}

步骤：fulltext

```kr-tool-example
{
  "tool": "get_content_detail",
  "arguments": {
    "url": "",
    "academic_reference": "0000000000000000000000000000000000000000000000000000000000000000"
  }
}
```
绑定：{"academic_reference": "search_academic.fulltext_ref.version_id"}

步骤：task_status

```kr-tool-example
{
  "tool": "get_task_status",
  "arguments": {
    "task_id": "<task_id>"
  }
}
```
绑定：{}

步骤：evidence_read

```kr-tool-example
{
  "tool": "read_knowledge",
  "arguments": {
    "request": {
      "action": "fetch",
      "version_id": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  }
}
```
绑定：{"request.version_id": "completed_task.evidence_assets.version_id"}

## 开放网页与新闻博客生态

生态：`generic_web_ecology`

开放网页搜索可发现官网、文档、新闻、博客和报告页面；摘要不能替代正文抽取。

候选工具：`kr_web_search`、`extract_web_page`、`extract_dynamic_page`

内容：官网、新闻、博客、文档、报告页面、网页文章

边界：搜索引擎索引不完整；摘要不能替代正文；动态页面可能需要升级抽取

## 招聘与职业社区生态

生态：`career_recruitment_ecology`

search_recruitment按来源返回职位或社区对象；详情与关系按对应适配器合同读取。

候选工具：`search_recruitment`、`get_content_detail`、`get_task_status`、`read_knowledge`

内容：结构化职位、职位详情、职业社区主题及可见讨论

边界：来源访问与账号边界各不相同；脉脉与V2EX讨论不是结构化职位卡
