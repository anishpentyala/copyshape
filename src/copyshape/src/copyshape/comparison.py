"""Compare two hypothetical routing plans over the same in-process scan."""
from .placement import plan


def compare(report, pools, before_routes, after_routes):
    """Attribute route changes with fixed pool identities and allocation units.

    Neither plan inspects a destination. A negative dense delta is a reduction
    in this model, not a promise of reclaimed space. Each physical source group
    contributes one preserved instance per destination pool containing its names.
    """
    before = plan(report, pools, before_routes)
    after = plan(report, pools, after_routes)

    def locate(path, routes):
        parent = path.rpartition('/')[0]
        while parent:
            if parent in routes:
                return routes[parent]
            parent = parent.rpartition('/')[0]
        return routes['']

    fields = ('file_names', 'hardlink_instances', 'payload_preserved',
              'payload_expanded', 'dense_preserved', 'dense_expanded')
    pool_changes = {
        name: {'before': before['pools'][name], 'after': after['pools'][name],
               'delta': {field: after['pools'][name][field] - before['pools'][name][field]
                         for field in fields}}
        for name in sorted(pools)
    }
    groups, moved_names = [], 0
    for group in report['groups']:
        old, new, moves = {}, {}, []
        for path in group['paths']:
            source, target = locate(path, before_routes), locate(path, after_routes)
            old.setdefault(source, []).append(path)
            new.setdefault(target, []).append(path)
            if source != target:
                moves.append({'path': path, 'before_pool': source, 'after_pool': target})
        if not moves:
            continue
        moved_names += len(moves)
        size = group['size_bytes']
        def dense(assignments):
            return sum(((size + pools[name] - 1) // pools[name]) * pools[name]
                       for name in assignments)
        groups.append({
            'size_bytes': size, 'moves': moves,
            'before_paths_by_pool': dict(sorted(old.items())),
            'after_paths_by_pool': dict(sorted(new.items())),
            'preserved_instances_delta': len(new) - len(old),
            'preserved_payload_delta_bytes': size * (len(new) - len(old)),
            'dense_preserved_delta_bytes': dense(new) - dense(old),
            'pools_gaining_instance': sorted(new.keys() - old.keys()),
            'pools_losing_instance': sorted(old.keys() - new.keys()),
        })
    return {
        'status': 'MODELLED_NOT_PHYSICAL_FORECAST',
        'before_routes': before['routes'], 'after_routes': after['routes'],
        'pools': pool_changes,
        'moved_file_names': moved_names,
        'groups_with_moved_names': groups,
        'total_dense_preserved_delta_bytes': after['total_dense_preserved_bytes'] - before['total_dense_preserved_bytes'],
        'total_dense_expanded_delta_bytes': after['total_dense_expanded_bytes'] - before['total_dense_expanded_bytes'],
        'cross_pool_additional_payload_delta_bytes': after['cross_pool_additional_payload_bytes'] - before['cross_pool_additional_payload_bytes'],
        'caveat': 'same selected source scan, fixed hypothetical pools and block sizes; deltas are not physical usage, transfer volume or reclaimable space',
    }
