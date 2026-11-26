"""Metadata-only capacity models. No copy, deletion or physical-space forecast."""
import os
import stat
from dataclasses import dataclass


class InvalidScan(ValueError):
    """The supported metadata model could not be established."""


@dataclass(frozen=True)
class Limits:
    max_entries: int = 100000
    max_depth: int = 128

    def __post_init__(self):
        if type(self.max_entries) is not int or not 1 <= self.max_entries <= 1000000:
            raise InvalidScan('max_entries must be an integer in 1..1000000')
        if type(self.max_depth) is not int or not 0 <= self.max_depth <= 256:
            raise InvalidScan('max_depth must be an integer in 0..256')


def _identity(info):
    return info.st_dev, info.st_ino


def _signature(info):
    return (info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, 'st_blocks', None))


def scan(root, *, block_size=4096, limits=None):
    """Walk one directory without following descendant symlinks.

    Requires POSIX descriptor-relative traversal. Final root symlinks are
    refused; ancestor resolution is the operating system's normal behavior.
    Result models a fresh destination without sparse/compression/reflink savings.
    """
    limits = Limits() if limits is None else limits
    if not isinstance(limits, Limits):
        raise InvalidScan('limits must be Limits')
    if type(block_size) is not int or not 1 <= block_size <= 2**30:
        raise InvalidScan('block_size must be an integer in 1..1073741824')
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or
            os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd or
            os.listdir not in os.supports_fd or os.scandir not in os.supports_fd):
        raise InvalidScan('POSIX descriptor-relative traversal is required')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    groups, directories, counts = {}, set(), {'entries': 0, 'directories': 0, 'symlinks': 0}

    def walk(fd, prefix, depth):
        before = os.fstat(fd)
        identity = _identity(before)
        if identity in directories:
            raise InvalidScan('repeated directory identity; scan refused')
        directories.add(identity)
        # Iterating with scandir avoids materializing an unbounded directory
        # listing before the entry cap can stop the walk.
        with os.scandir(fd) as iterator:
            for entry in iterator:
                counts['entries'] += 1
                if counts['entries'] > limits.max_entries:
                    raise InvalidScan('entry limit exceeded; no capacity result')
                name = entry.name
                path = name if not prefix else prefix + '/' + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    counts['symlinks'] += 1
                elif stat.S_ISDIR(info.st_mode):
                    if depth >= limits.max_depth:
                        raise InvalidScan('directory depth limit exceeded')
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        if _identity(os.fstat(child)) != _identity(info):
                            raise InvalidScan('directory changed during open')
                        counts['directories'] += 1
                        walk(child, path, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size < 0 or info.st_nlink < 1 or info.st_ino <= 0:
                        raise InvalidScan('unsupported regular-file metadata')
                    blocks = getattr(info, 'st_blocks', None)
                    if blocks is not None and (type(blocks) is not int or blocks < 0):
                        raise InvalidScan('invalid allocated-block metadata')
                    key = _identity(info)
                    if key in groups:
                        if groups[key]['signature'] != _signature(info):
                            raise InvalidScan('hardlink metadata changed during scan')
                        groups[key]['paths'].append(path)
                    else:
                        groups[key] = {'signature': _signature(info), 'paths': [path],
                                       'size': info.st_size, 'links': info.st_nlink,
                                       'allocated': None if blocks is None else blocks * 512}
                else:
                    raise InvalidScan('special file encountered; no capacity result')
        after = os.fstat(fd)
        if _signature(before) != _signature(after):
            raise InvalidScan('directory metadata changed during scan')

    try:
        root_path = os.fspath(root)
        if not isinstance(root_path, str) or not root_path:
            raise InvalidScan('root must be a nonempty text path')
        # A terminal slash or /. can otherwise make the kernel resolve a
        # symlink as a directory before O_NOFOLLOW sees the final component.
        while root_path.endswith('/') or root_path.endswith('/.'):
            if root_path == '/':
                break
            root_path = root_path[:-1] if root_path.endswith('/') else root_path[:-2]
            root_path = root_path or '/'
        root_fd = os.open(root_path, flags)
        try:
            walk(root_fd, '', 0)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise InvalidScan('filesystem access failed: ' + str(exc)) from exc
    rows = []
    for group in groups.values():
        paths = sorted(group['paths'])
        if len(paths) > group['links']:
            raise InvalidScan('observed names exceed link count; unstable scan')
        rounded = ((group['size'] + block_size - 1) // block_size) * block_size
        rows.append({'paths': paths, 'size_bytes': group['size'],
                     'selected_names': len(paths), 'reported_link_count': group['links'],
                     'unselected_link_count': group['links'] - len(paths),
                     'observed_allocated_bytes': group['allocated'],
                     'dense_preserved_bytes': rounded,
                     'dense_expanded_bytes': rounded * len(paths)})
    rows.sort(key=lambda row: row['paths'][0])
    payload_preserved = sum(row['size_bytes'] for row in rows)
    payload_expanded = sum(row['size_bytes'] * row['selected_names'] for row in rows)
    allocated_known = all(row['observed_allocated_bytes'] is not None for row in rows)
    return {'schema': 1, 'status': 'MODELLED_NOT_PHYSICAL_FORECAST',
            'block_size': block_size, 'scanned_entries': counts['entries'],
            'directories_below_root': counts['directories'], 'symlinks_not_followed': counts['symlinks'],
            'regular_file_names': sum(row['selected_names'] for row in rows),
            'unique_regular_files': len(rows),
            'observed_allocated_bytes': sum(row['observed_allocated_bytes'] for row in rows) if allocated_known else None,
            'logical_payload': {'hardlinks_preserved': payload_preserved, 'hardlinks_expanded': payload_expanded},
            'dense_block_model': {'hardlinks_preserved': sum(row['dense_preserved_bytes'] for row in rows),
                                  'hardlinks_expanded': sum(row['dense_expanded_bytes'] for row in rows)},
            'groups': rows,
            'assumptions': ['new empty destination', 'regular files only', 'dense per-file block rounding',
                            'selected hardlinks may be preserved or expanded'],
            'excluded': ['metadata and directory allocation', 'symlink storage', 'compression and reflinks',
                         'sparse preservation', 'existing destination reuse', 'free-space guarantee',
                         'atomic snapshot or complete concurrent-change detection']}
