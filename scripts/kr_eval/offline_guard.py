"""pytest 子进程禁用外网和真实浏览器启动；用例只能使用隔离替身。"""
import socket
import subprocess
import weakref


def install_network_policy():
    def blocked(*args, **kwargs):
        raise RuntimeError("EVAL_OFFLINE_POLICY: real network access is forbidden in fixture runs")
    original_bind=socket.socket.bind
    original_listen=socket.socket.listen
    original_connect=socket.socket.connect
    original_connect_ex=socket.socket.connect_ex
    original_resolve=socket.getaddrinfo
    listeners=weakref.WeakValueDictionary()
    def bind(sock,address):
        if not isinstance(address,tuple) or address[0] not in {'127.0.0.1','::1'} or address[1]!=0:
            return blocked()
        return original_bind(sock,address)
    def listen(sock,*args):
        result=original_listen(sock,*args)
        host,port=sock.getsockname()[:2]
        listeners[(host,port)]=sock
        return result
    def allowed(address):
        if not isinstance(address,tuple) or len(address)<2:
            return False
        host='127.0.0.1' if address[0]=='localhost' else address[0]
        listener=listeners.get((host,int(address[1])))
        return listener is not None and listener.fileno()>=0
    def connect(sock,address):
        return original_connect(sock,address) if allowed(address) else blocked()
    def connect_ex(sock,address):
        return original_connect_ex(sock,address) if allowed(address) else blocked()
    def resolve(host,port,*args,**kwargs):
        if not allowed((host,port)):
            return blocked()
        return original_resolve('127.0.0.1' if host=='localhost' else host,port,*args,**kwargs)
    socket.socket.bind=bind
    socket.socket.listen=listen
    socket.socket.connect=connect
    socket.socket.connect_ex=connect_ex
    socket.getaddrinfo=resolve


def pytest_configure(config):
    install_network_policy()
    original = subprocess.Popen
    class GuardedPopen(original):
        def __init__(self, args, *rest, **kwargs):
            text = str(args).lower()
            if any(name in text for name in ["chrome.exe", "msedge.exe", "firefox.exe", "curl", "wget", "playwright install", "yt-dlp", "powershell", "cmd.exe"]):
                raise RuntimeError("EVAL_OFFLINE_POLICY: external helper/browser launch forbidden")
            super().__init__(args, *rest, **kwargs)
    subprocess.Popen = GuardedPopen
