import json

from kr_core.source_urls import public_evidence_payload


def test_markdown_links_do_not_absorb_following_chinese_or_closing_delimiter():
    text='[自学](https://example.org/?q=Python)开源指南。\n\n![](https://pic.zhimg.com/a.jpg?source=abc)'
    assert public_evidence_payload(text)==text


def test_sensitive_markdown_links_remove_secrets_without_damaging_prose():
    text='[资料](https://user:password@example.org/a_(b)?token=private-marker&lang=zh#part)后面的中文。'
    expected='[资料](https://example.org/a_(b)?lang=zh)后面的中文。'
    assert public_evidence_payload(text)==expected
    assert public_evidence_payload(expected)==expected


def test_nested_payload_still_strips_credentials_and_signed_query_parameters():
    data={'cookie':'private-cookie','items':[{'url':'https://example.org/?xsec_token=private-marker&q=中文','text':'<https://example.org/?api_key=private-key&lang=zh>'}]}
    result=public_evidence_payload(data)
    assert 'private-' not in json.dumps(result)
    assert 'cookie' not in result
    assert result['items'][0]['text']=='<https://example.org/?lang=zh>'


def test_balanced_parentheses_in_bare_url_are_preserved():
    assert public_evidence_payload('链接 (https://example.org/a_(b)?q=x) 后续')=='链接 (https://example.org/a_(b)?q=x) 后续'
