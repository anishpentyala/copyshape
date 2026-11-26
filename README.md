# copyshape

A read-only copy/restore planning tool for hardlink expansion and dense-file capacity models.

A source tree's current disk usage is not the size of every possible copy. Hardlinks may become separate files, sparse files may be written densely, and placing two names on different destination filesystems prevents them from sharing a hardlink.

copyshape answers a narrower, explicit question: **how much regular-file payload and block-rounded dense storage does this selected tree model under different hardlink and placement choices?** It does not predict physical destination usage or promise that a restore will fit.

## Install and run

Python 3.10+ on Linux or another POSIX platform with descriptor-relative directory traversal is required. Windows is not supported. Tests run on Linux, not macOS.

```sh
python -m pip install .
copyshape ./source --block-size 4096 > report.json
```

There is no published package or release tag. Install from this checkout. The command outputs JSON only. Exit `0` means the model was computed, not that a copy is safe or will fit. Exit `2` means invalid input, unsupported metadata, a detected inconsistency, an exceeded limit, an access error or an output failure. On a scan or model error it emits no result; output failure itself can leave a partial output stream.

The scanner opens directories, not regular-file contents. It does not copy, delete, follow descendant symlinks or use the network. Reading directory entries may update directory access times according to the filesystem's mount policy. Reports include relative file names and can expose private paths, so keep them private.

## Small example

Suppose `data/a` and `data/b` are two selected hardlinks to one 4,097-byte file. With an assumed 4,096-byte destination allocation unit:

| Model | Bytes |
| --- | ---: |
| Logical payload, preserve selected hardlinks | 4,097 |
| Logical payload, expand each name | 8,194 |
| Dense block model, preserve selected hardlinks | 8,192 |
| Dense block model, expand each name | 16,384 |

An independent file with identical bytes is not a hardlink and is counted separately. Hardlinks to names outside the selected tree do not add payload to this model. Source allocation is reported separately using `st_blocks * 512` where available, counted once per `(st_dev, st_ino)` identity. It is not a measure of exclusive physical blocks or reclaimable space.

## Split a restore across destination pools

Destination pool names are hypothetical filesystem identities you supply, not mounted paths. Two names assigned to different pools cannot share a hardlink in the model. Different pool names mean distinct filesystems; using one name means hardlinks can be preserved within it.

```sh
copyshape ./source \
  --pool fast=4096 --pool archive=65536 \
  --route '=fast' --route 'archive=archive' \
  --route 'archive/hot=fast' > placement.json
```

The empty route supplies a default. A route matches a relative **directory prefix**, not a filename or glob. Longest matching directory wins, so `archive/hot/x` uses `fast`, `archive/old/x` uses `archive`, and `archive-old/x` uses the default. Slashes are POSIX separators even if a literal backslash occurs in a name. Prefixes must not contain empty, `.` or `..` components. Duplicate assignments and unknown pools are errors. An unused valid route is allowed.

The added `placement` object has per-pool payload and dense models, total dense models, and each hardlink family split across pools. `cross_pool_additional_payload_bytes` measures how much extra logical payload splitting requires compared with keeping each family on one filesystem. It is not the delta in physical disk usage. Pool allocation units can differ, so dense totals are calculated per pool, not by rounding a combined total.

## Python API

```python
from copyshape import Limits, scan
from copyshape.placement import plan

report = scan("./source", block_size=4096,
              limits=Limits(max_entries=100_000, max_depth=128))
placement = plan(report, {"fast": 4096, "archive": 65536},
                 {"": "fast", "archive": "archive"})
```

`plan` consumes an in-process `scan` result, not an arbitrary or edited JSON report. It performs no I/O. Both functions raise `InvalidScan` for supported validation failures. JSON schema version is `1`; unknown future fields should be ignored by readers.

## Bounds and refusal behavior

- Default scan cap: 100,000 entries including directories and skipped symlinks, and 128 directory levels below the root. Hard ceilings: 1,000,000 entries and depth 256. Limit failures return no capacity result, not a lower bound.
- Allocation units must be integers from 1 through 1,073,741,824 bytes. These are assumptions, not filesystem auto-detection. At most 256 pools and 10,000 routes are accepted.
- One directory root only. A final root symlink, including a trailing slash or `/.` spelling, is refused. Ancestor symlinks are resolved normally by the OS. Descendant directories are opened relative to directory descriptors with `O_NOFOLLOW`, then checked for matching device/inode identity.
- Descendant symlinks, including broken links and cycles, are counted but not followed. Their own storage is excluded. Sockets, FIFOs and device files cause refusal rather than silently disappearing from the model. Unreadable directories and metadata errors also cause refusal.
- Regular files are grouped by device **and** inode. Observed hardlink names must not exceed the reported link count, and repeated identities must have matching size, allocation, link count and timestamps. Directory metadata is compared before and after enumeration; repeated directory identities are refused.
- These checks detect some concurrent changes, not all. This is not an atomic snapshot, a hostile-tree security boundary, or a guarantee of a complete stable scan. Scan a quiescent tree or a filesystem snapshot for dependable planning. File mutation, inode reuse, coarse timestamps or a subtree move during traversal can escape the checks.
- Memory grows with selected paths. Directory enumeration is streaming and stops at the entry cap; output still contains each selected regular-file name. Very large accepted reports can consume substantial memory.

## What the numbers exclude

All destination numbers assume a fresh empty destination and dense regular files rounded independently to the supplied allocation unit. They exclude directory and symlink storage, filesystem metadata, journals, reserved space, allocation tails, compression, reflinks, deduplication, sparse preservation, archive format overhead and existing destination reuse. They do not inspect a destination, check capacity, reserve storage or implement the copy. A target can need more or fewer bytes than this model. Do not use the modeled totals alone as a restore go/no-go gate.

## Why another tool?

`du` measures source usage; `rsync` copies trees and can preserve hardlinks and sparse files; backup tools have their own format and destination semantics. copyshape is a small metadata-only comparison tool: it keeps those assumptions separate and attributes hardlink-family splits to explicit hypothetical destination pools. It is not a replacement for any of them, and it makes no claim that hardlink-aware size accounting itself is new.

Primary background:

- [rsync manual](https://rsync.samba.org/ftp/rsync/rsync.1.html): hardlink and sparse-copy options are separate choices.
- [mergerfs filesystem limits](https://trapexit.github.io/mergerfs/latest/faq/technical_behavior_and_limitations/): hardlinks cannot span distinct filesystems.
- [Python filesystem interfaces](https://docs.python.org/3/library/os.html#os.stat_result): stat metadata and descriptor-relative operations.
- [libarchive hardlinks](https://github.com/libarchive/libarchive/wiki/Hardlinks): device/inode tracking and format-dependent hardlink handling.

## Tests

```sh
python -m pip install . pytest
python -m pytest -q
```

Tests use real temporary directories, sparse files, hardlinks, out-of-tree names, odd filenames and symlinks. Randomized reference models independently check payload, block rounding and per-pool placement. Error tests cover scan limits, output failure and detected metadata changes. No benchmark or adoption claim is made.

MIT License. Copyright 2026 Anish Pentyala.
