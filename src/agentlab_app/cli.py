import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import yaml
from agentlab.abi import PluginDefinition
from agentlab.host import PluginHost
from agentlab.kernel import Registry
from .distribution import load,ROOT,CATALOG


def main(argv=None):
    parser=argparse.ArgumentParser(prog='agentlab')
    sub=parser.add_subparsers(dest='command',required=True)
    test=sub.add_parser('test');test.add_argument('--suite',choices=['basicagent','kernel-contracts','all'],default='all')
    test.add_argument('--offline',action='store_true',required=True);test.add_argument('--report-dir',default='reports/basicagent')
    test.add_argument('--browser-evidence',default='reports/browser/browser_validation.json')
    run=sub.add_parser('run');run.add_argument('--profile',default='viz-basic');run.add_argument('--report-dir',default='runs/cli');run.add_argument('--task',default='Read data.txt and report its line count')
    visual=sub.add_parser('visual');visual.add_argument('action',choices=['serve']);visual.add_argument('--profile',default='profiles/basicagent-viz.yaml')
    args=parser.parse_args(argv)
    if hasattr(args,'report_dir'):args.report_dir=str(Path(args.report_dir).resolve())
    if args.command=='test':
        import pytest
        report=Path(args.report_dir).resolve();report.mkdir(parents=True,exist_ok=True)
        os.environ['AGENTLAB_REPORT_DIR']=str(report)
        os.environ['AGENTLAB_BROWSER_EVIDENCE']=str(Path(args.browser_evidence).resolve())
        files=[]
        if args.suite in ('basicagent','all'):
            files.extend(str(ROOT/'tests'/name) for name in ('test_basicagent.py','test_application_http.py','test_live_workbench.py','test_provider_network.py'))
        if args.suite in ('kernel-contracts','all'):files.extend(str(ROOT/'tests'/('test_kernel_'+x+'.py')) for x in ('v1','contracts','control','wire'))
        raise SystemExit(pytest.main([*files,'-q','--disable-warnings','--junitxml='+str(report/'junit.xml')]))
    async def execute():
        if args.command=='run':
            host,p,names=await load(args.report_dir,args.profile)
            try:print(json.dumps(await host.run(payload={**p,'task':args.task}),ensure_ascii=False,indent=2))
            finally:await host.unload()
            return
        settings=yaml.safe_load(Path(args.profile).read_text())
        d=PluginDefinition.read(CATALOG/'basicagent/visual.web');registry=Registry();registry.register(d)
        host=PluginHost(registry)
        await host.load({'kernel_api':1,'composition':'agentlab-observatory','packages':[{'id':d.id,'version':d.version,'digest':d.digest}],
            'instances':{'web':{'plugin':d.id,'config':settings}},'entry':{'instance':'web','method':'start'}})
        try:
            print(json.dumps(await host.run(payload={})),flush=True)
            stop=asyncio.Event()
            for signum in (signal.SIGINT,signal.SIGTERM):asyncio.get_running_loop().add_signal_handler(signum,stop.set)
            await stop.wait()
        finally:await host.unload()
    asyncio.run(execute())


if __name__=='__main__':main()
