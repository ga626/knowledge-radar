"""关键命题、替代审阅、撤证和真实报告交付的脱敏合同反例。"""
import hashlib
import json
from pathlib import Path

import pytest
from test_research_archive import archive, evidence
from runtime.research_index import ResearchIndex, ResearchWrite
from runtime.research_ledger import record_tool_receipt
from runtime.research_contracts import parse_write
from runtime.research_archive import SubmitReport


def write(index, **kw):
    return index.annotate(parse_write({'task_id':'task-a',**kw}))


@pytest.fixture
def review(archive):
    engine,*_=archive
    index=ResearchIndex()
    index.annotate(ResearchWrite(action='question',task_id='task-a',question_id='q1',text='是否投入开发？'))
    refs=[]
    for n in range(2):
        ref=evidence(engine,'合成访谈：隐私偏好并不等于付费意愿。'+str(n),'https://example.org/e'+str(n))
        record_tool_receipt(task_id='task-a',trace_id='interview-'+str(n),tool='get_content_detail',status='ok',result={'evidence_assets':[ref]})
        refs.append({'version_id':ref['version_id'],'start':0,'end':18})
    write(index,action='task_purpose',record_id='purpose',purposes=['decision'],
        task_card='个人研究者输入散乱资料，输出可核验报告；本地部署，小团队预算。')
    claim=dict(action='claim',record_id='c1',question_id='q1',text='应先验证付费意愿，再决定开发',
        claim_type='recommendation',importance='high',applicability='个人研究者；尚未验证付费',basis='偏好证据不能支持投入承诺',
        dependencies=[{'group_id':'g1','role':'necessary','evidence_refs':[refs[0]],'basis':'唯一直接访谈'},
                      {'group_id':'g2','role':'support','evidence_refs':[refs[1]],'basis':'独立补充访谈'}],
        counterevidence_refs=[refs[1]],uncertainty='合成样本不代表市场')
    write(index,**claim)
    return engine,index,refs,claim


def projection(index,engine):
    from runtime.research_reviews import review_projection
    return review_projection(index,'task-a',assets=engine.assets)


def alternative(index,**kw):
    return write(index,action='alternatives_review',record_id='alt1',claim_id='c1',claim_revision=1,
        disposition='blocked',rationale='只有访谈，没有实际任务对比',search_scope='访谈资料',
        reopen_conditions='取得现成工具同任务结果',**kw)


def withdrawal(index,scenario,**kw):
    data=dict(action='withdrawal_review',record_id='withdraw-'+scenario['group_id'],claim_id='c1',claim_revision=1,
        scenario_id=scenario['scenario_id'],baseline= scenario['baseline'],conclusion='付费意愿仍未获得验证',
        change='weakened',rationale='撤除核心访谈后，不再具备实证支持',result_kind='hypothesis',remaining_refs=[])
    data.update(kw)
    return write(index,**data)


def test_decision_triggers_alternatives_and_each_core_group_withdrawal(review):
    engine,index,refs,_=review
    p=projection(index,engine)
    assert 'alternatives_unreviewed' in p['gaps']
    assert len(p['claims'][0]['scenarios'])==2
    first=p['claims'][0]['scenarios'][0]
    assert set(first['excluded_versions'])=={refs[0]['version_id']}
    assert set(first['remaining_versions'])=={refs[1]['version_id']}
    assert p['semantic_truth']=='not_program_verified'


def test_withdrawn_evidence_cannot_be_reused_to_claim_review_complete(review):
    engine,index,refs,_=review
    s=projection(index,engine)['claims'][0]['scenarios'][0]
    with pytest.raises(ValueError,match='excluded_evidence'):
        withdrawal(index,s,result_kind='empirical',remaining_refs=[refs[0]])
    with pytest.raises(ValueError,match='remaining_support_required'):
        withdrawal(index,s,result_kind='empirical')
    withdrawal(index,s)
    assert projection(index,engine)['claims'][0]['scenarios'][0]['review']['change']=='weakened'


def test_claim_revision_and_scope_change_invalidate_review(review):
    engine,index,_,claim=review
    old=projection(index,engine)['claims'][0]['scenarios'][0]
    withdrawal(index,old)
    write(index,**{**claim,'text':'修订后的有条件建议','expected_revision':1})
    with pytest.raises(ValueError,match='claim_revision_conflict'):
        withdrawal(index,old,record_id='stale')
    index.annotate(ResearchWrite(action='scope',task_id='task-a',question_id='q1',text='改为企业用户'))
    assert 'claim_scope_changed:c1' in projection(index,engine)['gaps']


def test_same_origin_group_removed_together_and_circular_claim_dependency_rejected(review):
    engine,index,refs,claim=review
    write(index,**{**claim,'dependencies':[{'group_id':'same-origin','role':'support','evidence_refs':refs,'basis':'同稿转载'}],'expected_revision':1})
    s=projection(index,engine)['claims'][0]['scenarios'][0]
    assert set(s['excluded_versions'])=={r['version_id'] for r in refs}
    assert s['remaining_versions']==[]
    with pytest.raises(ValueError,match='cyclic_claim_dependency'):
        write(index,**{**claim,'expected_revision':2,'dependencies':[{'group_id':'loop','role':'necessary','upstream_claim_id':'c1','upstream_revision':2,'basis':'自证'}]})


def test_cross_task_evidence_and_self_report_are_not_independent_support(review):
    engine,index,_,claim=review
    foreign=engine.assets.put('https://example.org/foreign','另一任务的材料')
    def dep(ref):return [{'group_id':'g','role':'support','evidence_refs':[{'version_id':ref['version_id'],'start':0,'end':2}],'basis':'测试'}]
    with pytest.raises(ValueError,match='not_registered_to_research_task'):
        write(index,**{**claim,'expected_revision':1,'dependencies':dep(foreign)})
    own=engine.assets.put('kr-report:own','自己的结论',kind='report')
    engine.assets.reference_many([own['version_id']],'task-a',task_id='task-a')
    with pytest.raises(ValueError,match='self_report_is_not_independent_evidence'):
        write(index,**{**claim,'expected_revision':1,'dependencies':dep(own)})


def test_legacy_empty_claims_and_all_background_never_count_as_reviewed(review):
    engine,index,refs,claim=review
    write(index,**{**claim,'expected_revision':1,'dependencies':[{'group_id':'bg','role':'background','evidence_refs':refs,'basis':'背景'}]})
    assert 'core_support_missing:c1' in projection(index,engine)['gaps']
    assert projection(index,engine)['review_status']!='recorded'
    assert projection(ResearchIndex(engine.root/'empty-index'),engine)['review_status']=='unreviewed'


def test_alternatives_not_found_is_not_absence_and_reuse_requires_snapshot(review):
    engine,index,_,_=review
    write(index,action='alternatives_review',record_id='not-found',claim_id='c1',claim_revision=1,
        disposition='not_found',rationale='只在已检索范围未确认',search_scope='仅合成访谈')
    p=projection(index,engine)
    assert p['claims'][0]['alternatives']['disposition']=='not_found'
    assert p['claims'][0]['alternatives']['absence_proven'] is False
    with pytest.raises(ValueError,match='reuse_snapshot_required'):
        write(index,action='alternatives_review',record_id='bad-reuse',claim_id='c1',claim_revision=1,
            disposition='reused',rationale='沿用',search_scope='当前任务')


def test_authorized_local_observation_has_real_file_hash_and_fixed_text(review,tmp_path):
    engine,index,_,_=review
    source=tmp_path/'observation.txt';source.write_text('本地实测：10次中有2次失败',encoding='utf-8')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    result=write(index,action='local_observation',record_id='obs',input_path=str(source),approved_input_root=str(tmp_path),
        input_sha256=digest,method='人工执行后保存的直接观察',title='本地实验')
    fixed=engine.assets.fetch(result['version_id'])
    assert fixed['text']==source.read_text(encoding='utf-8')
    source.write_text('后改内容',encoding='utf-8')
    assert engine.assets.fetch(result['version_id'])['text']!='后改内容'


def test_local_observation_keeps_project_scope_and_archives_full_source(review,tmp_path):
    engine,index,_,_=review
    text='合成观察原文：这不是报告摘要，必须单独完整归档。'*40
    source=tmp_path/'full-observation.txt';source.write_text(text,encoding='utf-8')
    result=write(index,action='local_observation',record_id='full-obs',input_path=str(source),
        approved_input_root=str(tmp_path),input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        method='直接读取隔离合成文件',title='完整观察原文')
    with engine.assets.connection() as conn:
        scopes=conn.execute('SELECT project_id FROM asset_scopes WHERE version_id=? AND task_id=?',
            (result['version_id'],'task-a')).fetchall()
    assert [row[0] for row in scopes]==['project-a']
    status=engine.run('task-a')
    assert status['source_delivery']['status']=='vault_committed'
    vault=engine._grant(engine._context('task-a')['vault_grant_id'],'project-a')
    assert any(text in path.read_text(encoding='utf-8') for path in vault.rglob('*.md') if path.name!='总报告.md')


def test_local_observation_retry_returns_same_fixed_version(review,tmp_path):
    engine,index,_,_=review
    source=tmp_path/'retry-observation.txt';source.write_text('合成重试输入',encoding='utf-8')
    request=dict(action='local_observation',record_id='retry-obs',input_path=str(source),approved_input_root=str(tmp_path),
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),method='合成输入幂等重试',title='重试观察')
    first=write(index,**request);second=write(index,**request)
    assert second['version_id']==first['version_id']
    assert second['revision']==first['revision']


def test_report_review_is_hash_bound_and_actual_delivery_contains_readable_table(review):
    engine,index,refs,_=review
    alternative(index)
    for s in projection(index,engine)['claims'][0]['scenarios']:withdrawal(index,s)
    content='# 研究结论\n建议先验证，尚不批准开发投入。'
    p=projection(index,engine)
    write(index,action='report_review',record_id='review1',report_sha256=hashlib.sha256(content.encode()).hexdigest(),
        baseline=p['baseline'],claim_ids=['c1'],coverage_statement='已反向核对报告关键建议；没有将背景写成事实')
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='审阅报告',content=content))
    assert result['summary']=='delivered'
    local=Path(result['local']['path']).read_bytes();vault=Path(result['vault']['path']).read_bytes()
    assert local==vault
    report=local.decode('utf-8')
    assert '关键结论与撤证审阅' in report
    assert '| 关键结论 | 核心依据 | 最强反证' in report
    assert '应先验证付费意愿' in report and '撤除核心访谈' in report
    assert 'claim_revision' not in report and 'remaining_refs' not in report
    assert '全文关键结论核对：已记录' in report
    assert '语义正确性：由 Codex 声明，程序未判真' in report
    assert engine.assets.fetch(result['report']['version_id'])['text']==report
    assert engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='审阅报告',content=content))['summary']=='delivered'


def test_report_change_cannot_reuse_old_full_text_review(review):
    engine,index,_,_=review
    p=projection(index,engine)
    with pytest.raises(ValueError,match='empty_claim_review_forbidden'):
        write(index,action='report_review',record_id='empty',report_sha256='a'*64,baseline=p['baseline'],claim_ids=[],coverage_statement='已读')
    write(index,action='report_review',record_id='first',report_sha256=hashlib.sha256(b'old').hexdigest(),baseline=p['baseline'],claim_ids=['c1'],coverage_statement='已核对')
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='改变后报告',content='新报告'))
    assert '全文关键结论核对：未审阅或已失效' in Path(result['local']['path']).read_text(encoding='utf-8')


def test_upstream_revision_change_marks_downstream_recheck(review):
    engine,index,_,claim=review
    write(index,**{**claim,'record_id':'c2','dependencies':[{'group_id':'up','role':'necessary','upstream_claim_id':'c1','upstream_revision':1,'basis':'建议依赖上游'}]})
    write(index,**{**claim,'expected_revision':1,'text':'上游改变'})
    assert 'upstream_claim_changed:c2' in projection(index,engine)['gaps']


def test_remaining_reference_cannot_expand_beyond_registered_support(review):
    engine,index,refs,_=review
    s=projection(index,engine)['claims'][0]['scenarios'][0]
    with pytest.raises(ValueError,match='outside_registered_support'):
        withdrawal(index,s,result_kind='empirical',remaining_refs=[{**refs[1],'end':19}])


def test_task_level_comparison_covers_same_task_and_purpose_revision_invalidates_it(review):
    engine,index,refs,_=review
    write(index,action='alternatives_review',record_id='task-alt',disposition='compared',rationale='同一输入输出对比',
        search_scope='两个现有方案的同任务试验',options=['现有工具：实测失败，权限不满足','人工整理：实测可行，但耗时高'],evidence_refs=refs)
    assert 'alternatives_unreviewed' not in projection(index,engine)['gaps']
    write(index,action='task_purpose',record_id='purpose',purposes=['decision'],task_card='改为企业任务',expected_revision=1)
    assert 'alternatives_unreviewed' in projection(index,engine)['gaps']


def test_public_read_review_is_bounded_and_shares_projection_identity(review):
    engine,index,_,_=review
    p=projection(index,engine)
    read=index.read('task-a',view='review',max_chars=8000)
    assert read['baseline']==p['baseline']
    assert len(json.dumps(read,ensure_ascii=False))<=8000


def test_upstream_scope_change_propagates_to_dependent_claims(review):
    engine,index,_,claim=review
    index.annotate(ResearchWrite(action='question',task_id='task-a',question_id='q2',text='下游建议'))
    write(index,**{**claim,'record_id':'c2','question_id':'q2','dependencies':[{'group_id':'up','role':'necessary','upstream_claim_id':'c1','upstream_revision':1,'basis':'依赖上游'}]})
    before=projection(index,engine)['claims'][1]['baseline']
    index.annotate(ResearchWrite(action='scope',task_id='task-a',question_id='q1',text='上游范围改变'))
    p=projection(index,engine)
    assert 'upstream_claim_changed:c2' in p['gaps']
    assert next(c for c in p['claims'] if c['record_id']=='c2')['baseline']!=before


def test_only_pass_or_whitespace_is_not_a_withdrawal_conclusion(review):
    engine,index,_,_=review
    s=projection(index,engine)['claims'][0]['scenarios'][0]
    for text in ('通过','  '):
        with pytest.raises(ValueError,match='conclusion_required'):
            withdrawal(index,s,conclusion=text)


def test_latest_review_uses_event_order_instead_of_record_name(review):
    engine,index,_,_=review
    alternative(index)
    write(index,action='alternatives_review',record_id='a-newer',claim_id='c1',claim_revision=1,
        disposition='not_applicable',rationale='用户仅希望先访谈，尚不做工具选型',search_scope='用户决策范围')
    assert projection(index,engine)['claims'][0]['alternatives']['record_id']=='a-newer'


def test_source_new_version_invalidates_withdrawal_snapshot(review):
    engine,index,_,_=review
    old=projection(index,engine)['claims'][0]['scenarios'][0]
    evidence(engine,'同来源后来更正：之前访谈信息无效。','https://example.org/e0')
    with pytest.raises(ValueError,match='stale_withdrawal_baseline'):
        withdrawal(index,old)
    assert 'source_version_changed:c1' in projection(index,engine)['gaps']


def test_source_update_also_invalidates_full_report_review(review):
    from runtime.research_reviews import report_appendix
    engine,index,_,_=review
    p=projection(index,engine);content='有条件的建议'
    write(index,action='report_review',record_id='before-update',report_sha256=hashlib.sha256(content.encode()).hexdigest(),
        baseline=p['baseline'],claim_ids=['c1'],coverage_statement='已核对全文')
    evidence(engine,'来源更正','https://example.org/e0')
    rendered,receipt=report_appendix(index,'task-a',content,engine.assets)
    assert receipt['full_text_review']=='unreviewed_or_stale'
    assert '全文关键结论核对：未审阅或已失效' in rendered


@pytest.mark.parametrize('case',json.loads((Path(__file__).parent/'fixtures/research_semantic_challenges.json').read_text(encoding='utf-8'))['cases'],ids=lambda c:c['id'])
def test_semantic_challenges_keep_codex_rejection_visible_in_delivery(review,case):
    engine,index,_,claim=review
    ref=evidence(engine,'\n'.join(case['texts']),'https://example.org/challenge/'+case['id'])
    write(index,**{**claim,'expected_revision':1,'text':case['claim'],'claim_type':'fact','support_assessment':case['assessment'],
        'basis':case['reason'],'dependencies':[{'group_id':'challenge','role':'support','evidence_refs':[{'version_id':ref['version_id'],'start':0,'end':len('\n'.join(case['texts']))}],'basis':case['reason']}]})
    p=projection(index,engine)
    assert 'semantic_support_insufficient:c1' in p['gaps']
    assert p['semantic_truth']=='not_program_verified'
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='语义挑战',content='# 待验证假设\n尚无可靠依据支持该强结论。'))
    text=Path(result['local']['path']).read_text(encoding='utf-8')
    assert case['reason'] in text and '语义依据不足' in text


def test_public_application_dispatch_updates_reads_and_delivers_same_review(review):
    from runtime import composition  # registers the public tool contracts
    from runtime.application import invoke
    engine,index,_,_=review
    result=invoke('update_research',{'request':{'task_id':'task-a','action':'alternatives_review','record_id':'app-alt',
        'claim_id':'c1','claim_revision':1,'disposition':'deferred','rationale':'先访谈，后做实测对比',
        'search_scope':'当前调研范围','reopen_conditions':'访谈结束'}})
    assert result['status']=='recorded'
    view=invoke('read_knowledge',{'request':{'action':'research','task_id':'task-a','view':'withdrawal_scenarios','limit':1}})
    assert view['items'][0]['claim_id']=='c1'
    assert view['truncated']
    next_page=invoke('read_knowledge',{'request':{'action':'research','task_id':'task-a','view':'withdrawal_scenarios',
        'after_seq':view['next_after_seq'],'before_seq':view['watermark'],'limit':1}})
    assert next_page['items'][0]['scenario_id']!=view['items'][0]['scenario_id']
    delivered=invoke('manage_research_artifacts',{'request':{'action':'submit_report','task_id':'task-a',
        'project_id':'project-a','title':'应用入口交付','content':'# 结论\n建议仍有条件。'}})
    assert delivered['summary']=='delivered'
    assert '直接替代比较推迟' in Path(delivered['vault']['path']).read_text(encoding='utf-8')


def test_report_and_review_excerpts_do_not_export_temporary_link_tokens(review):
    engine,index,refs,claim=review
    raw='访谈全文：[链接](https://example.org/?q=AI&token=synthetic-review-token)'
    ref=evidence(engine,raw,'https://example.org/private-parameters')
    write(index,**{**claim,'expected_revision':1,'dependencies':[{'group_id':'g','role':'support',
        'evidence_refs':[{'version_id':ref['version_id'],'start':0,'end':len(raw)}],'basis':'保留正文与定位'}]})
    content='# 报告\n[链接](https://example.org/?q=AI&token=synthetic-report-token)'
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='投影边界',content=content))
    text=Path(result['local']['path']).read_text(encoding='utf-8')
    assert 'synthetic-review-token' not in text and 'synthetic-report-token' not in text
    assert engine.assets.fetch(ref['version_id'])['text']==raw
    assert '链接投影已移除临时访问参数' in text


def test_report_projection_preserves_public_source_line_locations(review):
    engine,index,_,_=review
    content='# 原文定位\n[源码](https://github.com/example/repo/blob/main/a.py#L12-L18)'
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='定位保留',content=content))
    text=Path(result['local']['path']).read_text(encoding='utf-8')
    assert '#L12-L18)' in text
    assert not result['report']['review_snapshot']['projection_transforms']


def test_readable_audit_exposes_actual_alternatives_and_permitted_remaining_support(review):
    engine,index,refs,_=review
    write(index,action='alternatives_review',record_id='readable-alternatives',claim_id='c1',claim_revision=1,
        disposition='compared',rationale='只有访谈声明，尚无真实任务效果',search_scope='同一研究者任务',
        options=['维持现状：人工整理','现成工具：只验证宣称，适用性与效果未验证'],evidence_refs=[refs[1]])
    for scenario in projection(index,engine)['claims'][0]['scenarios']:withdrawal(index,scenario)
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='可读审阅',content='# 有条件建议'))
    text=Path(result['local']['path']).read_text(encoding='utf-8')
    assert '维持现状：人工整理' in text and '适用性与效果未验证' in text
    assert '比较依据：' in text
    assert '| 仍可用资料 |' in text
    assert '实际引用：未引用固定证据' in text


def test_readable_audit_separates_withdrawal_scenarios_and_uses_numbered_appendix(review):
    """人读总览不应把多个撤证情景塞进同一格，也不应泄漏机器定位。"""
    engine, index, refs, _ = review
    alternative(index, options=['维持现状：人工整理'])
    scenarios = projection(index, engine)['claims'][0]['scenarios']
    for scenario in scenarios:
        withdrawal(index, scenario)
    body = '# 结论\n建议先验证付费意愿，再决定开发。'
    p = projection(index, engine)
    write(index, action='report_review', record_id='readable-v2',
          report_sha256=hashlib.sha256(body.encode()).hexdigest(),
          baseline=p['baseline'], claim_ids=['c1'], coverage_statement='已核对')
    result = engine.submit(SubmitReport(action='submit_report', task_id='task-a',
                                        project_id='project-a', title='可读审阅新版', content=body))
    text = Path(result['local']['path']).read_text(encoding='utf-8')
    overview = text.split('## 撤证情景', 1)[0]
    assert overview.count('| 应先验证付费意愿，再决定开发 |') == 1
    assert '<br>' not in overview
    assert '## 撤证情景' in text
    scenario_section = text.split('## 撤证情景', 1)[1]
    assert scenario_section.count('| 结论1 |') == 2
    assert '撤去资料 [1]' in scenario_section
    assert '撤去资料 [2]' in scenario_section
    main, appendix = text.split('## 资料附录', 1)
    assert 'kr:' not in main
    assert refs[0]['version_id'] not in main
    assert refs[0]['version_id'] in appendix
    assert '## 资料附录' in text
    assert '资料1' in text and '资料2' in text
    assert appendix.count(refs[0]['version_id']) == 1
    assert appendix.count(refs[1]['version_id']) == 1
    assert Path(result['vault']['path']).read_bytes() == text.encode('utf-8')
    assert engine.assets.fetch(result['report']['version_id'])['text'] == text


@pytest.mark.parametrize('all_synthetic', [False, True])
def test_readable_audit_deduplicates_mixed_synthetic_disclosures_and_keeps_scope(review, all_synthetic):
    """合成声明统一披露，真实与合成资料不能被错误地整体标记。"""
    engine, index, refs, claim = review
    marker = '【合成数据，非真实市场证据】'
    synthetic_raw = marker + '偏好不能证明付费。'
    synthetic = evidence(engine, synthetic_raw, 'https://example.org/synthetic')
    other_raw = (marker if all_synthetic else '') + '另一来源摘录。'
    real = evidence(engine, other_raw, 'https://example.org/real')
    record_tool_receipt(task_id='task-a', trace_id='real-1', tool='get_content_detail', status='ok',
                        result={'evidence_assets': [real]})
    write(index, **{**claim, 'expected_revision': 1,
                    'dependencies': [
                        {'group_id': 'g1', 'role': 'necessary',
                         'evidence_refs': [{'version_id': synthetic['version_id'], 'start': 0, 'end': len(synthetic_raw)}], 'basis': '首份材料'},
                        {'group_id': 'g2', 'role': 'support',
                         'evidence_refs': [{'version_id': real['version_id'], 'start': 0, 'end': len(other_raw)}],
                         'basis': '另一份材料'}], 'counterevidence_refs': []})
    p = projection(index, engine)
    body = '# 结论\n仍需验证。'
    write(index, action='report_review', record_id='mixed-v2',
          report_sha256=hashlib.sha256(body.encode()).hexdigest(), baseline=p['baseline'],
          claim_ids=['c1'], coverage_statement='已核对')
    result = engine.submit(SubmitReport(action='submit_report', task_id='task-a', project_id='project-a',
                                        title='混合资料审阅', content=body))
    text = Path(result['local']['path']).read_text(encoding='utf-8')
    assert text.count('合成资料编号：') == 1
    assert '资料1' in text
    assert '资料2' in text
    assert '合成资料编号：资料1' in text
    assert ('合成资料编号：资料1、资料2。' in text) == all_synthetic
    assert text.count('合成数据，非真实市场证据') == 1
    assert engine.assets.fetch(synthetic['version_id'])['text'] == synthetic_raw
    assert '资料2（合成）' not in text
    assert '适用范围：个人研究者；尚未验证付费' in text
    assert '语义正确性：由 Codex 声明，程序未判真' in text
