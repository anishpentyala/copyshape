import builtins
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from copyshape import InvalidScan, Limits, scan
from copyshape.cli import main


def put(root, name, size):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as stream:
        stream.truncate(size)
    return path


def test_empty(tmp_path):
    result = scan(tmp_path)
    assert result['logical_payload'] == {'hardlinks_preserved': 0, 'hardlinks_expanded': 0}
    assert result['dense_block_model'] == result['logical_payload']
    assert result['groups'] == []


@pytest.mark.parametrize('size', [0, 1, 511, 512, 513, 4095, 4096, 4097, 2**32 + 1])
@pytest.mark.parametrize('block', [1, 512, 4096, 10000])
def test_rounding(tmp_path, size, block):
    put(tmp_path, 'a', size)
    result = scan(tmp_path, block_size=block)
    assert result['logical_payload']['hardlinks_preserved'] == size
    assert result['dense_block_model']['hardlinks_preserved'] == ((size + block - 1) // block) * block
    assert result['groups'][0]['observed_allocated_bytes'] == (tmp_path / 'a').stat().st_blocks * 512


def test_hardlinks_and_outside_names(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    source = put(root, 'a', 4097)
    os.link(source, root / 'b')
    os.link(source, tmp_path / 'outside')
    result = scan(root)
    assert result['regular_file_names'] == 2
    assert result['unique_regular_files'] == 1
    assert result['logical_payload'] == {'hardlinks_preserved': 4097, 'hardlinks_expanded': 8194}
    assert result['dense_block_model'] == {'hardlinks_preserved': 8192, 'hardlinks_expanded': 16384}
    group, = result['groups']
    assert group['paths'] == ['a', 'b']
    assert group['unselected_link_count'] == 1
    assert result['observed_allocated_bytes'] == source.stat().st_blocks * 512


def test_identical_contents_are_not_hardlinks(tmp_path):
    put(tmp_path, 'a', 20)
    put(tmp_path, 'b', 20)
    result = scan(tmp_path)
    assert result['unique_regular_files'] == 2
    assert result['logical_payload']['hardlinks_preserved'] == 40


def test_no_symlink_follow(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    put(outside, 'large', 10**6)
    (root / 'dirlink').symlink_to(outside, target_is_directory=True)
    (root / 'filelink').symlink_to(outside / 'large')
    (root / 'broken').symlink_to(outside / 'missing')
    (root / 'cycle').symlink_to(root, target_is_directory=True)
    result = scan(root)
    assert result['symlinks_not_followed'] == 4
    assert result['logical_payload']['hardlinks_expanded'] == 0


@pytest.mark.parametrize('suffix', ['', '/', '/.', '/./'])
def test_root_symlink_refused(tmp_path, suffix):
    root = tmp_path / 'root'
    root.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(InvalidScan):
        scan(str(alias) + suffix)


@pytest.mark.parametrize('kind', ['missing', 'file', 'fifo'])
def test_unsupported(tmp_path, kind):
    if kind == 'file':
        path = put(tmp_path, 'plain', 1)
    elif kind == 'fifo':
        os.mkfifo(tmp_path / 'pipe')
        path = tmp_path
    else:
        path = tmp_path / 'missing'
    with pytest.raises(InvalidScan):
        scan(path)


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '4096', None, 2**30 + 1])
def test_invalid_block(tmp_path, value):
    with pytest.raises(InvalidScan):
        scan(tmp_path, block_size=value)


@pytest.mark.parametrize('value', [True, 0, -1, 1000001, 1.5, '1', None])
def test_invalid_entry_limits(value):
    with pytest.raises(InvalidScan):
        Limits(max_entries=value)


@pytest.mark.parametrize('value', [True, -1, 257, 1.5, '1', None])
def test_invalid_depth_limits(value):
    with pytest.raises(InvalidScan):
        Limits(max_depth=value)


def test_limits(tmp_path):
    put(tmp_path, 'a', 1)
    assert scan(tmp_path, limits=Limits(max_entries=1))['scanned_entries'] == 1
    put(tmp_path, 'b', 1)
    with pytest.raises(InvalidScan, match='entry limit'):
        scan(tmp_path, limits=Limits(max_entries=1))
    (tmp_path / 'dir').mkdir()
    with pytest.raises(InvalidScan, match='depth'):
        scan(tmp_path, limits=Limits(max_depth=0))


def test_nested_and_weird_names(tmp_path, capsys):
    put(tmp_path, 'dir/a\n\x1b[31m', 3)
    assert main([str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert '\x1b' not in output
    result = json.loads(output)
    assert result['directories_below_root'] == 1
    assert result['groups'][0]['paths'] == ['dir/a\n\x1b[31m']


def test_non_utf8(tmp_path, capsys):
    name = os.fsdecode(b'bad\xff')
    put(tmp_path, name, 1)
    assert main([str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert output.isascii()
    assert json.loads(output)['groups'][0]['paths'] == [name]


def test_no_content_reads_or_modifications(tmp_path, monkeypatch):
    source = put(tmp_path, 'file', 8)
    before = source.stat()
    real_open = os.open
    calls = []
    def guarded(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        calls.append(path)
        return real_open(path, flags, *args, **kwargs)
    # Preserve capability checks while intercepting actual opens.
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {guarded})
    monkeypatch.setattr(os, 'open', guarded)
    monkeypatch.setattr(builtins, 'open', lambda *a, **kw: pytest.fail('content open'))
    scan(tmp_path)
    assert calls == [str(tmp_path)] or calls == [tmp_path]
    after = source.stat()
    assert (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)


def test_cli_no_partial_result(tmp_path, capsys):
    os.mkfifo(tmp_path / 'pipe')
    assert main([str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert 'special file' in output.err


def test_output_failure(tmp_path, monkeypatch):
    class Broken:
        def write(self, text):
            raise OSError('full')
    monkeypatch.setattr(sys, 'stdout', Broken())
    assert main([str(tmp_path)]) == 2


@pytest.mark.parametrize('seed', range(20))
def test_independent_tree_oracle(tmp_path, seed):
    rng = random.Random(seed)
    sizes, multiplicities = [], []
    for i in range(rng.randrange(1, 12)):
        size = rng.randrange(0, 20000)
        count = rng.randrange(1, 6)
        source = put(tmp_path, f'd{i}/0', size)
        for j in range(1, count):
            os.link(source, tmp_path / f'd{i}/{j}')
        sizes.append(size)
        multiplicities.append(count)
    block = rng.choice([1, 512, 4096, 10000])
    result = scan(tmp_path, block_size=block)
    assert result['logical_payload']['hardlinks_preserved'] == sum(sizes)
    assert result['logical_payload']['hardlinks_expanded'] == sum(s*c for s, c in zip(sizes, multiplicities))
    # Deliberately use divmod rather than the implementation formula.
    dense = [((s // block) + bool(s % block)) * block for s in sizes]
    assert result['dense_block_model']['hardlinks_preserved'] == sum(dense)
    assert result['dense_block_model']['hardlinks_expanded'] == sum(s*c for s, c in zip(dense, multiplicities))


def test_unstable_directory_refused(tmp_path, monkeypatch):
    real_scandir = os.scandir
    class Changed:
        def __enter__(self):
            self.context = real_scandir(tmp_path)
            self.iterator = self.context.__enter__()
            put(tmp_path, 'new', 1)
            before = tmp_path.stat()
            os.utime(tmp_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
            return self.iterator
        def __exit__(self, *args):
            return self.context.__exit__(*args)
    def changed_scandir(fd):
        return Changed()
    monkeypatch.setattr(os, 'supports_fd', os.supports_fd | {changed_scandir})
    monkeypatch.setattr(os, 'scandir', changed_scandir)
    with pytest.raises(InvalidScan, match='directory metadata changed'):
        scan(tmp_path)


def test_real_dense_copy_reference(tmp_path):
    import shutil
    root = tmp_path / 'source'
    root.mkdir()
    source = put(root, 'sparse', 1024 * 1024 + 1)
    os.link(source, root / 'hardlink')
    (root / 'empty').touch()
    report = scan(root)
    expanded = tmp_path / 'expanded'
    preserved = tmp_path / 'preserved'
    # GNU cp is an external reference for link semantics; physical block use
    # is deliberately not asserted to equal the model on every filesystem.
    if shutil.which('cp') is None:
        pytest.skip('cp unavailable')
    subprocess.run(['cp', '-R', '--sparse=never', '--reflink=never', str(root), str(expanded)], check=True)
    subprocess.run(['cp', '-a', '--sparse=never', '--reflink=never', str(root), str(preserved)], check=True)
    assert (expanded / 'sparse').stat().st_ino != (expanded / 'hardlink').stat().st_ino
    assert (preserved / 'sparse').stat().st_ino == (preserved / 'hardlink').stat().st_ino
    for tree, label in [(expanded, 'hardlinks_expanded'), (preserved, 'hardlinks_preserved')]:
        seen = set()
        payload = 0
        for path in tree.iterdir():
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                payload += info.st_size
        assert report['logical_payload'][label] == payload


def test_symlink_swap_before_directory_open(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    put(outside, 'hidden', 100)
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'child').mkdir()
    original = os.open
    def swapped(path, flags, *args, **kwargs):
        if path == 'child':
            (root / 'child').rmdir()
            (root / 'child').symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {swapped})
    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(InvalidScan, match='filesystem access'):
        scan(root)


def test_hardlink_inconsistent_metadata(tmp_path, monkeypatch):
    import types
    first = put(tmp_path, 'a', 1)
    os.link(first, tmp_path / 'b')
    original = os.stat
    calls = [0]
    def changed(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if kwargs.get('dir_fd') is not None:
            calls[0] += 1
            if calls[0] == 2:
                fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                fields['st_size'] += 1
                return types.SimpleNamespace(**fields)
        return info
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {changed})
    monkeypatch.setattr(os, 'stat', changed)
    with pytest.raises(InvalidScan, match='hardlink metadata changed'):
        scan(tmp_path)


def test_descriptor_cleanup_on_failure(tmp_path):
    if not Path('/proc/self/fd').exists():
        pytest.skip('Linux fd inspection unavailable')
    put(tmp_path, 'deep/file', 1)
    before = len(list(Path('/proc/self/fd').iterdir()))
    for _ in range(10):
        with pytest.raises(InvalidScan):
            scan(tmp_path, limits=Limits(max_entries=1))
    assert len(list(Path('/proc/self/fd').iterdir())) == before
