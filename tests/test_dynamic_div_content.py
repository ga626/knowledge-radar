from generic_web.collector import collect_rendered_html
from generic_web.models import GenericWebRequest


def test_rendered_div_and_span_content_is_not_an_empty_shell():
    body = "智能文档解析，PDF秒变高质量Markdown文件。精准解析表格、图片、公式等内容。" * 9
    html = f'<html><title>PaddleOCR</title><body><div id="main"><div class="ocr-content"><span>{body}</span></div></div><script src="main.js"></script></body></html>'
    result = collect_rendered_html(GenericWebRequest(url="https://aistudio.baidu.com/paddleocr"), html=html, final_url="https://aistudio.baidu.com/paddleocr", status_code=200)
    assert not result.error
    assert body in result.content


def test_tiny_semantic_widget_does_not_hide_real_div_body():
    body = "文档解析支持多语言识别，完整还原文档层级与排版结构。" * 16
    html = f'<body><div class="post-icon">分享</div><div id="main">{body}</div><script src="main.js"></script></body>'
    result = collect_rendered_html(GenericWebRequest(url="https://example.com/app"), html=html, final_url="https://example.com/app", status_code=200)
    assert not result.error
    assert body in result.content


def test_article_extract_preserves_single_code_block_and_table_cells():
    from generic_web.collector import _html_to_markdown, _normalize_markdown
    from bs4 import BeautifulSoup
    html='''<article><h1>文档</h1><p>保留<code>getFiles()</code>示例。</p>
    <pre><code>if ready:
    save_note()


else:
    wait()</code></pre>
    <table><tr><th>模型</th><th>价格</th></tr><tr><td>A</td><td>5</td></tr></table></article>'''
    body=_normalize_markdown(_html_to_markdown(BeautifulSoup(html,'lxml').article))
    assert body.count('save_note()')==1
    assert 'if ready:\n    save_note()\n\n\nelse:\n    wait()' in body
    assert '`getFiles()`' in body and '| 模型 | 价格 |' in body and '| A | 5 |' in body


def test_source_page_ui_removed_but_authored_javascript_example_retained():
    body='这是正式文章中的有效说明。'*30
    html=f'<html><body><nav>全站菜单</nav><main><h1>开发说明</h1><p>{body}</p><pre><code>const html = "&lt;script&gt;example()&lt;/script&gt;";</code></pre></main><footer>网站页脚</footer><script>tracking()</script></body></html>'
    result=collect_rendered_html(GenericWebRequest(url='https://example.org/guide'),html=html)
    assert not result.error
    assert '全站菜单' not in result.content and '网站页脚' not in result.content and 'tracking()' not in result.content
    assert '<script>example()</script>' in result.content and body in result.content
