def test_native_timeout_preserves_original_operation_and_phase(tmp_path, monkeypatch):
    from runtime.native_site import NativeSite
    site = NativeSite('maimai','脉脉','https://maimai.cn','https://maimai.cn',lambda _: False)
    monkeypatch.setattr(NativeSite, 'state_path', lambda _: tmp_path / 'interaction.json')
    class Bridge:
        def request(self, *a, **kw):
            return {'ok':False,'code':'native_command_timeout','request_id':'original',
                'request_executed':True,'data':{'browser_diagnostics':{
                    'last_stage':'adapter_started','stage_elapsed_ms':1200,
                    'command_elapsed_ms':20000,'secret':'must not propagate'}}}
    monkeypatch.setattr(NativeSite, 'bridge', lambda _: Bridge())
    result = site.invoke('detail', url='https://maimai.cn/article/detail')
    assert result['failure_type'] == 'request_outcome_unknown'
    assert result['operation_id'] == 'original'
    assert result['metadata']['browser_diagnostics']['last_stage'] == 'adapter_started'
    assert 'secret' not in result['metadata']['browser_diagnostics']
    assert site.state()['operation_id'] == 'original'


def test_health_carries_route_and_generation_observation(tmp_path, monkeypatch):
    import json
    from contextlib import nullcontext
    import httpx
    from runtime.browser_link import BrowserLink
    path=tmp_path/'state.json'
    path.write_text(json.dumps({'port':1234,'token':'fixture','generation':'expected','profile_id':'private-profile'}))
    bridge=BrowserLink(state_file=path)
    monkeypatch.setattr(bridge,'distribution_status',lambda:None)
    class Client:
        def get(self,*a,**kw):
            return httpx.Response(200,json={'connected':True,'protocol':1,'generation':'different'})
    monkeypatch.setattr(bridge,'_session',lambda:nullcontext(Client()))
    result=bridge.health()
    assert result['ok'] is False
    observation=result['observation']
    assert observation['driver']=='content_script_native_messaging'
    assert observation['expected_generation']=='expected'
    assert observation['observed_at']>0
    assert observation['profile_id_hash'] and 'private-profile' not in json.dumps(observation)
