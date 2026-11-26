import os
import random

import pytest

from copyshape import InvalidScan, scan
from copyshape.cli import main
from copyshape.placement import plan
from test_core import put


def test_pool_split(tmp_path):
    source = put(tmp_path, 'a/one', 4097)
    (tmp_path / 'b').mkdir()
    os.link(source, tmp_path / 'a/two')
    os.link(source, tmp_path / 'b/three')
    result = plan(scan(tmp_path), {'small': 512, 'big': 4096}, {'': 'small', 'b': 'big'})
    assert result['pools']['small']['dense_preserved'] == 4608
    assert result['pools']['small']['dense_expanded'] == 9216
    assert result['pools']['big']['dense_preserved'] == 8192
    assert result['total_dense_preserved_bytes'] == 12800
    assert result['total_dense_expanded_bytes'] == 17408
    assert result['cross_pool_additional_payload_bytes'] == 4097
    assert result['hardlink_groups_split_across_pools'][0]['paths_by_pool'] == {'big': ['b/three'], 'small': ['a/one', 'a/two']}


def test_boundary_and_longest_prefix(tmp_path):
    for name in ['a/z', 'a/b/z', 'ab/z', 'a/bc/z']:
        put(tmp_path, name, 1)
    result = plan(scan(tmp_path), {'x': 1, 'y': 2, 'z': 3}, {'': 'x', 'a': 'y', 'a/b': 'z'})
    assert result['pools']['x']['file_names'] == 1
    assert result['pools']['y']['file_names'] == 2
    assert result['pools']['z']['file_names'] == 1


@pytest.mark.parametrize('pools,routes', [({}, {'': 'x'}), ({'x': 0}, {'': 'x'}), ({'': 1}, {'': ''}),
    ({'x': True}, {'': 'x'}), ({'x': 1}, {}), ({'x': 1}, {'': 'y'}),
    ({'x': 1}, {'': 'x', '/a': 'x'}), ({'x': 1}, {'': 'x', 'a/../b': 'x'}),
    ({'x': 1}, {'': 'x', 'a//b': 'x'}), ({'x': 1}, {'': 'x', 'a/': 'x'}),
    ({'x': 1}, {'': 'x', '.': 'x'})])
def test_invalid(tmp_path, pools, routes):
    with pytest.raises(InvalidScan):
        plan(scan(tmp_path), pools, routes)


@pytest.mark.parametrize('args', [['--pool', 'x=1'], ['--route', '=x'], ['--pool', 'x=1', '--pool', 'x=2', '--route', '=x'],
    ['--pool', 'x=no', '--route', '=x'], ['--pool', 'x', '--route', '=x'],
    ['--pool', 'x=1', '--route', '=x', '--route', '=x']])
def test_cli_invalid(tmp_path, capsys, args):
    assert main([str(tmp_path)] + args) == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize('seed', range(20))
def test_placement_oracle(tmp_path, seed):
    rng = random.Random(seed)
    expected = {pool: {'payload': 0, 'dense': 0, 'expanded': 0} for pool in ['a', 'b', 'c']}
    split_payload = 0
    for i in range(12):
        size = rng.randrange(20000)
        locations = rng.choices(['a', 'b', 'c'], k=rng.randrange(1, 5))
        first = None
        for j, pool in enumerate(locations):
            name = f'{pool}/g{i}-{j}'
            if first is None:
                first = put(tmp_path, name, size)
            else:
                (tmp_path / pool).mkdir(exist_ok=True)
                os.link(first, tmp_path / name)
        split_payload += size * (len(set(locations)) - 1)
        for pool in set(locations):
            expected[pool]['payload'] += size
            rounded = sum(512 for _ in range((size // 512) + bool(size % 512)))
            expected[pool]['dense'] += rounded
            expected[pool]['expanded'] += rounded * locations.count(pool)
    result = plan(scan(tmp_path), {pool: 512 for pool in expected}, {'': 'a', 'b': 'b', 'c': 'c'})
    for pool, values in expected.items():
        assert result['pools'][pool]['payload_preserved'] == values['payload']
        assert result['pools'][pool]['dense_preserved'] == values['dense']
        assert result['pools'][pool]['dense_expanded'] == values['expanded']
    assert result['cross_pool_additional_payload_bytes'] == split_payload
