"""Place a completed metadata scan onto hypothetical destination pools."""
from .core import InvalidScan


def plan(report, pools, routes):
    """Model selected file names on named pools via longest-prefix routing.

    pools: {name: allocation_unit_bytes}. routes: {relative_directory: pool}.
    The empty prefix is mandatory. Directory boundaries matter, so `a` never
    matches `ab`. No real destination is inspected and no space is reserved.
    """
    if not isinstance(pools, dict) or not pools or len(pools) > 256:
        raise InvalidScan('pools must contain 1..256 named block sizes')
    for name, block in pools.items():
        if not isinstance(name, str) or not name or len(name) > 256:
            raise InvalidScan('pool names must contain 1..256 characters')
        if type(block) is not int or not 1 <= block <= 2**30:
            raise InvalidScan('pool block sizes must be integers in 1..1073741824')
    if not isinstance(routes, dict) or '' not in routes or len(routes) > 10000:
        raise InvalidScan('routes must include a default empty prefix, at most 10000 routes')
    for prefix, pool in routes.items():
        if not isinstance(prefix, str) or (prefix and any(part in ('', '.', '..') for part in prefix.split('/'))):
            raise InvalidScan('routes must be clean relative POSIX directory prefixes')
        if not isinstance(pool, str) or pool not in pools:
            raise InvalidScan('route names an unknown pool')
    def destination(path):
        # Directory-parent lookup avoids entries times routes comparisons.
        parent = path.rpartition('/')[0]
        while parent:
            if parent in routes:
                return routes[parent]
            parent = parent.rpartition('/')[0]
        return routes['']
    outputs = {name: {'block_size': block, 'file_names': 0, 'hardlink_instances': 0,
                      'payload_preserved': 0, 'payload_expanded': 0,
                      'dense_preserved': 0, 'dense_expanded': 0} for name, block in sorted(pools.items())}
    split_groups = []
    # Only accept the scanner's typed group representation, not arbitrary
    # untrusted saved reports. The CLI passes the in-process scan directly.
    if not isinstance(report, dict) or report.get('schema') != 1 or not isinstance(report.get('groups'), list):
        raise InvalidScan('expected a copyshape scan report')
    for group in report['groups']:
        assigned = {}
        for path in group['paths']:
            assigned.setdefault(destination(path), []).append(path)
        if len(assigned) > 1:
            split_groups.append({'paths_by_pool': dict(sorted(assigned.items())),
                                 'additional_payload_bytes': (len(assigned) - 1) * group['size_bytes']})
        for pool, paths in assigned.items():
            output = outputs[pool]
            size = group['size_bytes']
            rounded = ((size + pools[pool] - 1) // pools[pool]) * pools[pool]
            output['file_names'] += len(paths)
            output['hardlink_instances'] += 1
            output['payload_preserved'] += size
            output['payload_expanded'] += size * len(paths)
            output['dense_preserved'] += rounded
            output['dense_expanded'] += rounded * len(paths)
    return {'status': 'MODELLED_NOT_PHYSICAL_FORECAST', 'routes': dict(sorted(routes.items())),
            'pools': outputs, 'hardlink_groups_split_across_pools': split_groups,
            'cross_pool_additional_payload_bytes': sum(g['additional_payload_bytes'] for g in split_groups),
            'total_dense_preserved_bytes': sum(o['dense_preserved'] for o in outputs.values()),
            'total_dense_expanded_bytes': sum(o['dense_expanded'] for o in outputs.values()),
            'caveat': 'preserving hardlinks only coalesces selected names within each pool; all scan exclusions still apply'}
