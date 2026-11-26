import argparse
import json
import sys

from .core import InvalidScan, Limits, scan
from .placement import plan


def _assignments(items, *, numbers=False):
    result = {}
    for item in items:
        if '=' not in item:
            raise InvalidScan('assignments require KEY=VALUE')
        key, value = item.rsplit('=', 1)
        if key in result:
            raise InvalidScan('duplicate assignment key')
        result[key] = int(value) if numbers else value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only dense-copy and hardlink expansion models, not a physical-space forecast.')
    parser.add_argument('root', help='one directory; final root symlinks refused')
    parser.add_argument('--block-size', type=int, default=4096, help='assumed destination allocation unit in bytes (default: 4096)')
    parser.add_argument('--max-entries', type=int, default=100000)
    parser.add_argument('--max-depth', type=int, default=128)
    parser.add_argument('--pool', action='append', default=[], metavar='NAME=BLOCK_BYTES', help='hypothetical destination pool; repeatable')
    parser.add_argument('--route', action='append', default=[], metavar='PREFIX=POOL', help='relative directory route; include =POOL as default')
    args = parser.parse_args(argv)
    try:
        if bool(args.pool) != bool(args.route):
            raise InvalidScan('--pool and --route must be provided together')
        pools = _assignments(args.pool, numbers=True) if args.pool else None
        routes = _assignments(args.route) if args.route else None
        result = scan(args.root, block_size=args.block_size,
                      limits=Limits(args.max_entries, args.max_depth))
        if pools is not None:
            result['placement'] = plan(result, pools, routes)
        # ASCII encoding keeps newlines, terminal controls and undecodable
        # POSIX names inside quoted JSON, rather than writing raw path bytes.
        sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + '\n')
        sys.stdout.flush()
    except (InvalidScan, OSError, ValueError) as exc:
        sys.stderr.write('copyshape: ' + ascii(str(exc)) + '\n')
        return 2
    return 0


def entrypoint():
    raise SystemExit(main())
