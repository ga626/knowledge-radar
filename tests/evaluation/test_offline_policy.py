"""隔离策略须能测自己创建的服务，同时拒绝访问日用服务和外网。"""
from pathlib import Path
import subprocess
import sys


def test_only_fixture_owned_loopback_listener_is_allowed():
    root=Path(__file__).resolve().parents[2]
    code='''
import socket
from scripts.kr_eval.offline_guard import install_network_policy
install_network_policy()
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0));listener.listen(1)
    with socket.create_connection(listener.getsockname(),timeout=1) as client:
        peer,_=listener.accept();peer.close()
for address in [('127.0.0.1',18882),('127.0.0.1',18883),('93.184.216.34',443),('example.org',443)]:
    try:
        socket.create_connection(address,timeout=.1)
    except RuntimeError as error:
        assert 'EVAL_OFFLINE_POLICY' in str(error)
    else:
        raise AssertionError('escaped isolation')
print('PASS')
'''
    bootstrap='import sys;sys.path.insert(0,'+repr(str(root))+');'+code
    result=subprocess.run([sys.executable,'-X','utf8','-c',bootstrap],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='PASS'
