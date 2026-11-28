import json
import os
import random

import pytest

from copyshape import InvalidScan, scan
from copyshape.cli import main
from copyshape.comparison import compare
from test_core import put


def test_split_join_and_reverse(tmp_path):
    source = put(tmp_path, 'a/one', 4097)
    (tmp_path / 'b').mkdir()
    os.link(source, tmp_path / 'b/two')
    pools = {'small': 512, 'big': 4096}
    joined, split = {'': 'small'}, {'': 'small', 'b': 'big'}
    result = compare(scan(tmp_path), pools, joined, split)
    assert result['moved_file_names'] == 1
    assert result['total_dense_preserved_delta_bytes'] == 8192
    assert result['total_dense_expanded_delta_bytes'] == 8192 - 4608
    assert result['cross_pool_additional_payload_delta_bytes'] == 4097
    group = result['groups_with_moved_names'][0]
    assert group['preserved_instances_delta'] == 1
    assert group['pools_gaining_instance'] == ['big']
    assert group['pools_losing_instance'] == []
    reverse = compare(scan(tmp_path), pools, split, joined)
    for key in ('total_dense_preserved_delta_bytes', 'total_dense_expanded_delta_bytes',
                'cross_pool_additional_payload_delta_bytes'):
        assert reverse[key] == -result[key]


def test_move_same_instance_count_can_change_rounding(tmp_path):
    put(tmp_path, 'one', 4097)
    report = scan(tmp_path)
    result = compare(report, {'a': 512, 'b': 4096}, {'': 'a'}, {'': 'b'})
    group = result['groups_with_moved_names'][0]
    assert group['preserved_instances_delta'] == 0
    assert group['preserved_payload_delta_bytes'] == 0
    assert group['dense_preserved_delta_bytes'] == 3584
    assert group['pools_losing_instance'] == ['a']
    assert group['pools_gaining_instance'] == ['b']


def test_identical_routing_and_zero_size_moves(tmp_path):
    put(tmp_path, 'a/zero', 0)
    report = scan(tmp_path)
    same = compare(report, {'x': 4096}, {'': 'x'}, {'': 'x', 'a': 'x'})
    assert same['groups_with_moved_names'] == []
    assert same['moved_file_names'] == 0
    moved = compare(report, {'x': 4096, 'y': 65536}, {'': 'x'}, {'': 'y'})
    assert moved['moved_file_names'] == 1
    assert moved['total_dense_preserved_delta_bytes'] == 0


@pytest.mark.parametrize('routes', [{}, {'': 'missing'}, {'': 'x', '/a': 'x'}, {'': 'x', 'a/../b': 'x'}])
def test_invalid_second_plan(tmp_path, routes):
    with pytest.raises(InvalidScan):
        compare(scan(tmp_path), {'x': 4096}, {'': 'x'}, routes)


@pytest.mark.parametrize('args', [
    ['--compare-route', '=x'],
    ['--pool', 'x=4096', '--route', '=x', '--compare-route', '=x', '--compare-route', '=x'],
    ['--pool', 'x=4096', '--route', '=x', '--compare-route', 'a=x'],
    ['--pool', 'x=4096', '--route', '=x', '--compare-route', '=bad'],
])
def test_cli_refuses_incomplete_comparison(tmp_path, capsys, args):
    assert main([str(tmp_path)] + args) == 2
    assert capsys.readouterr().out == ''


def test_cli_one_scan_and_no_new_io(tmp_path, capsys, monkeypatch):
    import copyshape.cli as cli
    put(tmp_path, 'a/one', 3)
    calls = []
    def once(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1
        return scan(*args, **kwargs)
    monkeypatch.setattr(cli, 'scan', once)
    assert main([str(tmp_path), '--pool', 'x=1', '--pool', 'y=8', '--route', '=x',
                 '--compare-route', '=x', '--compare-route', 'a=y']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['comparison']['total_dense_preserved_delta_bytes'] == 5
    assert len(calls) == 1


@pytest.mark.parametrize('seed', range(30))
def test_randomized_independent_oracle(tmp_path, seed):
    rng = random.Random(seed)
    block = {'a': 1, 'b': 512, 'c': 4096}
    old_routes = {'': 'a', 'd1': 'b', 'd2': 'c'}
    new_routes = {'': 'c', 'd1': 'c', 'd2': 'a'}
    old_totals = {p: [0, 0] for p in block}
    new_totals = {p: [0, 0] for p in block}
    old_lookup = {'d0': 'a', 'd1': 'b', 'd2': 'c'}
    new_lookup = {'d0': 'c', 'd1': 'c', 'd2': 'a'}
    moved = 0
    for i in range(15):
        size = rng.randrange(10000)
        dirs = rng.choices(['d0', 'd1', 'd2'], k=rng.randrange(1, 5))
        first = None
        for j, d in enumerate(dirs):
            name = f'{d}/{i}-{j}'
            if first is None:
                first = put(tmp_path, name, size)
            else:
                (tmp_path / d).mkdir(exist_ok=True)
                os.link(first, tmp_path / name)
            moved += old_lookup[d] != new_lookup[d]
        for lookup, totals in ((old_lookup, old_totals), (new_lookup, new_totals)):
            assigned = [lookup[d] for d in dirs]
            for p in set(assigned):
                rounded = (size // block[p] + bool(size % block[p])) * block[p]
                totals[p][0] += rounded
                totals[p][1] += rounded * assigned.count(p)
    result = compare(scan(tmp_path), block, old_routes, new_routes)
    assert result['moved_file_names'] == moved
    for p in block:
        assert result['pools'][p]['delta']['dense_preserved'] == new_totals[p][0] - old_totals[p][0]
        assert result['pools'][p]['delta']['dense_expanded'] == new_totals[p][1] - old_totals[p][1]
    assert sum(g['dense_preserved_delta_bytes'] for g in result['groups_with_moved_names']) == result['total_dense_preserved_delta_bytes']


def test_boundary_deep_prefix_and_literal_backslash(tmp_path):
    for name in ['a/x', 'a/deep/x', 'ab/x', 'a\\b/x']:
        put(tmp_path, name, 1)
    result = compare(scan(tmp_path), {'x': 1, 'y': 2}, {'': 'x'},
                     {'': 'x', 'a': 'y', 'a/deep': 'x'})
    assert result['moved_file_names'] == 1
    assert result['groups_with_moved_names'][0]['moves'][0]['path'] == 'a/x'


def test_empty_tree_and_report_not_mutated(tmp_path):
    import copy
    report = scan(tmp_path)
    before = copy.deepcopy(report)
    result = compare(report, {'x': 1, 'y': 2}, {'': 'x'}, {'': 'y'})
    assert result['moved_file_names'] == 0
    assert result['total_dense_preserved_delta_bytes'] == 0
    assert report == before


def test_output_failure_still_nonzero(tmp_path, monkeypatch):
    import copyshape.cli as cli
    class BrokenOutput:
        def write(self, value):
            raise OSError('simulated output failure')
    monkeypatch.setattr(cli.sys, 'stdout', BrokenOutput())
    assert main([str(tmp_path), '--pool', 'x=1', '--route', '=x', '--compare-route', '=x']) == 2
