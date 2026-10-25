"""可选媒体适配的只读安装计划或显式持久任务提交。"""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from runtime.optional_media import COMPONENTS,component_plan,validate_receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','install','submit'])
    parser.add_argument('--component',required=True,choices=list(COMPONENTS))
    parser.add_argument('--payload-file',type=Path)
    args=parser.parse_args()
    if args.action=='plan':result=component_plan(args.component)
    elif args.action == 'install':
        if not args.payload_file: parser.error('install requires reviewed offline installation --payload-file')
        from runtime.optional_media import install_component
        result = install_component(args.component, json.loads(args.payload_file.read_text(encoding='utf-8-sig')))
    else:
        if not args.payload_file:parser.error('submit requires explicit --payload-file')
        validate_receipt(args.component)
        payload=json.loads(args.payload_file.read_text(encoding='utf-8-sig'))
        payload['component']=args.component
        from runtime.task_service import TaskService
        result=TaskService().submit('optional_media',payload,timeout_s=payload.get('timeout_s',600))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
