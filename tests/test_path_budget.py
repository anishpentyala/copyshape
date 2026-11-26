import json
import os

import pytest

from copyshape import InvalidScan, Limits, scan
from copyshape.cli import main


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.0, '1', None, 268435457])
def test_invalid_path_limit(value):
    with pytest.raises(InvalidScan, match='max_path_bytes'):
        Limits(max_path_bytes=value)


def test_exact_boundary_and_empty_tree(tmp_path):
    assert scan(tmp_path, limits=Limits(max_path_bytes=1))['scanned_relative_path_bytes'] == 0
    (tmp_path / 'abc').touch()
    assert scan(tmp_path, limits=Limits(max_path_bytes=3))['scanned_relative_path_bytes'] == 3
    with pytest.raises(InvalidScan, match='path byte limit'):
        scan(tmp_path, limits=Limits(max_path_bytes=2))


def test_all_entry_kinds_and_hardlink_names_count(tmp_path):
    (tmp_path / 'd').mkdir()
    (tmp_path / 'd' / 'a').touch()
    os.link(tmp_path / 'd' / 'a', tmp_path / 'b')
    (tmp_path / 'link').symlink_to('missing')
    # d + d/a + b + link, with each hardlink name counted separately.
    assert scan(tmp_path, limits=Limits(max_path_bytes=9))['scanned_relative_path_bytes'] == 9
    with pytest.raises(InvalidScan, match='path byte limit'):
        scan(tmp_path, limits=Limits(max_path_bytes=8))


@pytest.mark.parametrize('raw', [b'abc', 'éé'.encode(), b'bad\xff', b'a\n\x1b'])
def test_raw_posix_bytes_not_json_or_character_count(tmp_path, raw):
    name = os.fsdecode(raw)
    (tmp_path / name).touch()
    report = scan(tmp_path, limits=Limits(max_path_bytes=len(raw)))
    assert report['scanned_relative_path_bytes'] == len(raw)
    assert report['groups'][0]['paths'] == [name]
    with pytest.raises(InvalidScan, match='path byte limit'):
        scan(tmp_path, limits=Limits(max_path_bytes=len(raw) - 1))


def test_repeated_directory_prefix_counts_for_each_entry(tmp_path):
    (tmp_path / 'long').mkdir()
    for name in ('a', 'b', 'c'):
        (tmp_path / 'long' / name).touch()
    assert scan(tmp_path, limits=Limits(max_path_bytes=22))['scanned_relative_path_bytes'] == 22
    with pytest.raises(InvalidScan, match='path byte limit'):
        scan(tmp_path, limits=Limits(max_path_bytes=21))


def test_refuses_before_stat_of_over_budget_name(tmp_path, monkeypatch):
    (tmp_path / 'too-long').touch()
    original = os.stat
    def guarded(path, *args, **kwargs):
        if path == 'too-long':
            pytest.fail('over-budget entry was inspected')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {guarded})
    monkeypatch.setattr(os, 'stat', guarded)
    with pytest.raises(InvalidScan, match='path byte limit'):
        scan(tmp_path, limits=Limits(max_path_bytes=1))


def test_cli_budget_no_partial_report(tmp_path, capsys):
    (tmp_path / 'abc').touch()
    assert main([str(tmp_path), '--max-path-bytes', '2']) == 2
    result = capsys.readouterr()
    assert result.out == ''
    assert 'path byte limit' in result.err
    assert main([str(tmp_path), '--max-path-bytes', '3']) == 0
    assert json.loads(capsys.readouterr().out)['scanned_relative_path_bytes'] == 3


def test_positional_limits_compatibility():
    assert Limits(10, 2).max_path_bytes == 16777216
    assert Limits(max_path_bytes=268435456).max_path_bytes == 268435456


@pytest.mark.parametrize('value', ['0', '-1', '268435457'])
def test_cli_invalid_budget_before_scan(tmp_path, capsys, value):
    assert main([str(tmp_path / 'missing'), '--max-path-bytes', value]) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'max_path_bytes' in captured.err


def test_refusal_closes_directory_descriptors(tmp_path):
    if not os.path.isdir('/proc/self/fd'):
        pytest.skip('Linux fd accounting fixture')
    (tmp_path / 'd').mkdir()
    (tmp_path / 'd' / 'too-long').touch()
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(40):
        with pytest.raises(InvalidScan, match='path byte limit'):
            scan(tmp_path, limits=Limits(max_path_bytes=2))
    assert len(os.listdir('/proc/self/fd')) == before


def test_model_totals_unchanged_by_budget(tmp_path):
    (tmp_path / 'a').write_bytes(b'data')
    os.link(tmp_path / 'a', tmp_path / 'b')
    assert scan(tmp_path) == scan(tmp_path, limits=Limits(max_path_bytes=2))
