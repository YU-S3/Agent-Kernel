"""Generic composition CLI: no implicit application entry or algorithm."""
import argparse
import asyncio
import json
from pathlib import Path
from .kernel import Registry, PluginDefinition, PluginHost, Composition


async def execute(args):
    registry = Registry()
    for root in args.package:
        registry.register(PluginDefinition.read(root))
    host = PluginHost(registry, audit_path=args.audit)
    await host.load(Composition.read(args.composition), expected_lock=json.loads(Path(args.expect_lock).read_text()) if args.expect_lock else None)
    try:
        if args.lock:
            path = Path(args.lock)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as stream:
                json.dump(host.lock, stream, indent=2)
        if args.operation == 'run':
            entry = None
            if args.entry:
                instance, method = args.entry.rsplit('.', 1)
                entry = {'instance':instance,'method':method}
            result = await host.run(payload=json.loads(args.input), entry=entry)
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps({'state':host.state,'digest':host.lock['digest']}))
    finally:
        await host.unload()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['load','run'])
    parser.add_argument('composition')
    parser.add_argument('--package',action='append',default=[])
    parser.add_argument('--entry')
    parser.add_argument('--input',default='null')
    parser.add_argument('--lock')
    parser.add_argument('--expect-lock')
    parser.add_argument('--audit')
    asyncio.run(execute(parser.parse_args()))


if __name__=='__main__':
    main()
