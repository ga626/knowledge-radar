"""兼容的MCP启动入口；业务合同在runtime.composition中装配。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime import composition
from runtime.mcp_server import mount

mount(composition)
if __name__ == "__main__":
    composition.main()
else:
    # 沿用旧Python调用方的依赖注入入口；owner不导入此门面。
    sys.modules[__name__] = composition
