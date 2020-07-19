# This file is part of the pylint-ignore project
# https://gitlab.com/mbarkhau/pylint-ignore
#
# Copyright (c) 2020 Manuel Barkhau (mbarkhau@gmail.com) - MIT License
# SPDX-License-Identifier: MIT
import re
import shutil
import typing as typ
import logging
import collections

import pylev
import pathlib2 as pl

logger = logging.getLogger('pylint_ignore')


ENTRY_TEMPLATE = """
## File {entry.path} - Line {lineno} - {entry.msg_id} ({entry.symbol})

- `message: {entry.msg_text}`
- `author : {entry.author}`
- `date   : {entry.date}`

```
{ctx_src_text}
```


"""

# https://regex101.com/r/ogknXY/6
_ENTRY_HEADER_PATTERN = r"""
^
    \#\#\s
    File\s(?P<path>.*)
    \s-\s
    Line\s(?P<lineno>\d+)
    \s-\s
    (?P<msg_id>\w\d+)
    \s
    \((?P<symbol>.*)\)
$
"""

ENTRY_HEADER_RE = re.compile(_ENTRY_HEADER_PATTERN, flags=re.VERBOSE)


# https://regex101.com/r/6JViif/5
_LIST_ITEM_PATTERN = r"""
^
\s*-\s
`
(?P<key>message|author|date)
\s*:\s
(?P<value>.*)
`
$
"""

LIST_ITEM_RE = re.compile(_LIST_ITEM_PATTERN, flags=re.VERBOSE)


# https://regex101.com/r/Cc8w4v/5
_SOURCE_TEXT_PATTERN = r"""
(```|~~~)(?P<language>\w+)?
    (
        (?:\s+(?P<def_lineno>\d+):\s(?P<def_line>.*))?
        \s+\.\.\.
    )?
    (?:\s+\d+:\s?.*)?
    (?:\s+\d+:\s?.*)?
    \s*\>\s+(?P<source_lineno>\d+):\s(?P<source_line>.*)
    (?:\s+\d+:\s?.*)?
    (?:\s+\d+:\s?.*)?
    \s*
(```|~~~)
"""

SOURCE_TEXT_RE = re.compile(_SOURCE_TEXT_PATTERN, flags=re.VERBOSE)


class SourceText(typ.NamedTuple):

    new_lineno  : int
    old_lineno  : int
    source_line : str
    text        : str
    start_idx   : int
    end_idx     : int
    def_line_idx: typ.Optional[int]
    def_line    : typ.Optional[str]


# SourceText is almost always Optional
MaybeSourceText = typ.Optional[SourceText]


class Key(typ.NamedTuple):
    """Stable (relatively) key to reference ignorefile.Entry values.

    The ignorefile key is relatively stable, even between edits
    to a file. In particular, it doesn't have the lineno.
    """

    msg_id     : str
    path       : str
    symbol     : str
    msg_text   : str
    source_line: str


class Entry(typ.NamedTuple):

    msg_id  : str
    path    : str
    symbol  : str
    msg_text: str

    author: str
    date  : str
    srctxt: MaybeSourceText


class ObsoleteEntry(Exception):
    pass


Catalog = typ.Dict[Key, Entry]


FUZZY_MATCH_MAX_EDIT_DISTANCE_ABS = 4
FUZZY_MATCH_MAX_EDIT_DISTANCE_PCT = 20


def find_entry(catalog: Catalog, search_key: Key) -> typ.Optional[Entry]:
    has_exact_match = search_key in catalog
    if has_exact_match:
        # exact match
        return catalog[search_key]

    # try for a fuzzy match
    candidate_keys = [
        key
        for key in catalog.keys()
        if (
            search_key.msg_id     == key.msg_id
            and search_key.path   == key.path
            and search_key.symbol == key.symbol
        )
    ]

    matches: typ.List[Entry] = []
    for key in candidate_keys:
        msg_text_dist = pylev.levenshtein(key.msg_text   , search_key.msg_text)
        src_line_dist = pylev.levenshtein(key.source_line, search_key.source_line)

        if msg_text_dist > FUZZY_MATCH_MAX_EDIT_DISTANCE_ABS:
            continue
        if src_line_dist > FUZZY_MATCH_MAX_EDIT_DISTANCE_ABS:
            continue

        msg_text_dist_pct = 100 * msg_text_dist / max(len(key.msg_text), len(search_key.msg_text))
        src_line_dist_pct = (
            100 * src_line_dist / max(len(key.source_line), len(search_key.source_line))
        )

        if msg_text_dist_pct > FUZZY_MATCH_MAX_EDIT_DISTANCE_PCT:
            continue
        if src_line_dist_pct > FUZZY_MATCH_MAX_EDIT_DISTANCE_PCT:
            continue

        matches.append(catalog[key])

    if len(matches) == 1:
        return matches[0]
    else:
        return None


CONTEXT_LINES = 2


_SRC_CACHE: typ.Dict[str, typ.List[str]] = {}


def read_source_lines(path: str) -> typ.List[str]:
    if path not in _SRC_CACHE:
        if len(_SRC_CACHE) > 2:
            _SRC_CACHE.popitem()

        with pl.Path(path).open(mode="r", encoding="utf-8") as fobj:
            full_src_text = fobj.read()

        _keepends = True
        lines     = full_src_text.splitlines(_keepends)
        _SRC_CACHE[path] = lines

    return _SRC_CACHE[path]


def find_source_text_lineno(path: str, old_source_line: str, old_lineno: int) -> int:
    old_line_idx = old_lineno - 1
    lines        = read_source_lines(path)

    # NOTE (mb 2020-07-17): It's not too critical that we find the original
    #       entry. If we don't (and the message is still valid) then it will
    #       just be replaced by a new entry which will have to be acknowledged
    #       again. The git diff should make very obvious what happened.

    for offset in range(100):
        for line_idx in {old_line_idx - offset, old_line_idx + offset}:
            is_matching_line = (
                0 <= line_idx < len(lines) and lines[line_idx].rstrip() == old_source_line.rstrip()
            )
            if is_matching_line:
                return line_idx + 1

    raise ObsoleteEntry("source text not found")


def read_source_text(path: str, new_lineno: int, old_lineno: int) -> SourceText:
    lines           = read_source_lines(path)
    line_idx        = new_lineno - 1  # lineno starts at 1
    line_indent_lvl = len(lines[line_idx]) - len(lines[line_idx].lstrip())

    start_idx = max(0, line_idx - CONTEXT_LINES)
    end_idx   = min(len(lines), line_idx + CONTEXT_LINES + 1)
    src_lines = lines[start_idx:end_idx]
    src_text  = "".join(src_lines)

    source_line = lines[line_idx]
    def_line_idx: typ.Optional[int] = None
    def_line    : typ.Optional[str] = None

    maybe_def_idx = line_idx

    while maybe_def_idx > 0:
        line_text  = lines[maybe_def_idx]
        indent_lvl = len(line_text) - len(line_text.lstrip())
        if line_text.strip() and indent_lvl < line_indent_lvl:
            first_token = line_text.lstrip().split()[0]
            if first_token in ('def', 'class'):
                is_defline_before_ctx_src = 0 <= maybe_def_idx < start_idx
                if is_defline_before_ctx_src:
                    def_line_idx = maybe_def_idx
                    def_line     = lines[maybe_def_idx]
                break

        maybe_def_idx -= 1

    return SourceText(
        new_lineno, old_lineno, source_line, src_text, start_idx, end_idx, def_line_idx, def_line
    )


IGNOREFILE_HEADER = """# `pylint-ignore`

**WARNING: This file is programatically generated.**

This file is parsed by `pylint-ignore` to determine which `pylint`
messages should be ignored.

- Do not edit this file manually.
- To update, use `pylint-ignore --update-ignorefile`

The recommended approach to using `pylint-ignore` is:

1. If a message refers to a valid issue, update your code rather than
   ignoring the message.
2. If a message should *always* be ignored (globally), then to do so
   via the usual `pylintrc` or `setup.cfg` files rather than this
  `pylint-ignore.md` file.
3. If a message is a false positive, add a comment of this form to your code:
   `# pylint:disable=<symbol> ; explanation why this is a false positive`

"""


EntryValues = typ.Dict[str, str]


def _init_entry_item(entry_vals: EntryValues) -> typ.Tuple[Key, Entry]:
    old_ctx_src_text      = entry_vals['ctx_src_text']
    old_source_text_match = SOURCE_TEXT_RE.match(old_ctx_src_text)
    if old_source_text_match is None:
        raise ObsoleteEntry("Invalid source text")

    path = entry_vals['path']

    # NOTE (mb 2020-07-16): The file may have changed in the meantime,
    #    so we search for the original source text (which may be on a
    #    different line).
    old_source_line = old_source_text_match.group('source_line')

    old_lineno = int(entry_vals['lineno'])
    srctxt: MaybeSourceText = None
    try:
        new_lineno  = find_source_text_lineno(path, old_source_line, old_lineno)
        srctxt      = read_source_text(path, new_lineno, old_lineno)
        source_line = srctxt.source_line
    except ObsoleteEntry:
        source_line = old_source_line

    ignorefile_entry = Entry(
        entry_vals['msg_id'],
        entry_vals['path'],
        entry_vals['symbol'],
        entry_vals['message'],
        entry_vals['author'],
        entry_vals['date'],
        srctxt,
    )
    ignorefile_key = Key(
        ignorefile_entry.msg_id,
        ignorefile_entry.path,
        ignorefile_entry.symbol,
        ignorefile_entry.msg_text,
        source_line,
    )
    return (ignorefile_key, ignorefile_entry)


def _dumps_entry(entry: Entry) -> str:
    srctxt = entry.srctxt
    if srctxt is None:
        lineno       = -1
        ctx_src_text = ""
    else:
        lineno          = srctxt.new_lineno
        last_ctx_lineno = srctxt.end_idx + 1
        padding_size    = len(str(last_ctx_lineno))

        src_lines: typ.List[str] = []

        def_line     = srctxt.def_line
        def_line_idx = srctxt.def_line_idx
        if def_line and def_line_idx:
            def_lineno = def_line_idx + 1
            line       = def_line.rstrip()
            src_lines.append(f"  {def_lineno:>{padding_size}}: {line}")
            if def_lineno + CONTEXT_LINES < srctxt.new_lineno:
                src_lines.append("  ...")

        for offset, line in enumerate(srctxt.text.splitlines()):
            src_lineno = srctxt.start_idx + offset + 1
            # padded_line is to avoid trailing whitespace
            padded_line = " " + line if line.strip() else ""
            if lineno == src_lineno:
                dumps_line = f"> {src_lineno:>{padding_size}}:{padded_line}"
            else:
                dumps_line = f"  {src_lineno:>{padding_size}}:{padded_line}"
            src_lines.append(dumps_line)

        ctx_src_text = "\n".join(src_lines)

    entry_text = ENTRY_TEMPLATE.format(entry=entry, lineno=lineno, ctx_src_text=ctx_src_text)
    return entry_text.lstrip("\n")


def _parse_ctx_src_text(fence: str, lines: typ.Iterator[typ.Tuple[int, str]]) -> str:
    ctx_src_text_lines = [fence + "\n"]
    while True:
        # consume lines to next fence
        _, next_line = next(lines)
        ctx_src_text_lines.append(next_line)
        is_close_fence = next_line.strip() == fence
        if is_close_fence:
            break
    return "".join(ctx_src_text_lines)


def _iter_entry_values(ignorefile_path: pl.Path) -> typ.Iterable[EntryValues]:
    entry_vals: EntryValues = {}

    with ignorefile_path.open(mode="r", encoding="utf-8") as fobj:
        lines = iter(enumerate(fobj))
        try:
            while True:
                i, line = next(lines)
                ignorefile_lineno = i + 1

                if line.startswith("```"):
                    fence = line[:3]
                    entry_vals['ctx_src_text'] = _parse_ctx_src_text(fence, lines)
                    continue

                entry_header = ENTRY_HEADER_RE.match(line)
                if entry_header and 'msg_id' in entry_vals:
                    # new header -> any existing entry is done
                    yield entry_vals
                    # new entry
                    entry_vals = {}

                if entry_header:
                    entry_vals['ignorefile_lineno'] = str(ignorefile_lineno)
                    entry_vals.update(entry_header.groupdict())
                    assert 'msg_id' in entry_vals
                    continue

                list_item = LIST_ITEM_RE.match(line)
                if list_item:
                    entry_vals[list_item.group('key')] = list_item.group('value')
        except StopIteration:
            pass

    # yield last entry (not followed by a header that would otherwise trigger the yield)
    if 'msg_id' in entry_vals:
        yield entry_vals


def load(ignorefile_path: pl.Path) -> Catalog:
    if not ignorefile_path.exists():
        return {}

    catalog: Catalog = collections.OrderedDict()
    for entry_vals in _iter_entry_values(ignorefile_path):
        try:
            ignorefile_key, ignorefile_entry = _init_entry_item(entry_vals)
            catalog[ignorefile_key] = ignorefile_entry
        except ObsoleteEntry:
            # NOTE (mb 2020-07-17): It is fine for an entry to be obsolete.
            #   The code may have improved, it may have moved, in any case
            #   the ignore file is under version control and the change
            #   will be seen.
            pass
        except (KeyError, ValueError) as ex:
            lineno = entry_vals['ignorefile_lineno']
            path   = entry_vals['path']
            logmsg = f"Error parsing entry on line {lineno} of {path}: {ex}"
            logger.error(logmsg, exc_info=True)

    return catalog


def dumps(ignorefile: Catalog) -> str:
    ignorefile_chunks = [IGNOREFILE_HEADER]
    entries           = [e for e in ignorefile.values() if e.srctxt]
    entries.sort(key=lambda e: (e.msg_id, e.srctxt and e.srctxt.new_lineno, e.msg_text))

    for entry in entries:
        ignorefile_chunks.append(_dumps_entry(entry))

    return "".join(ignorefile_chunks)


def dump(ignorefile: Catalog, ignorefile_path: pl.Path) -> None:
    ignorefile_text = dumps(ignorefile)
    tmp_path        = ignorefile_path.parent / (ignorefile_path.name + ".tmp")
    with tmp_path.open(mode="w", encoding="utf-8") as fobj:
        fobj.write(ignorefile_text)
    shutil.move(str(tmp_path), str(ignorefile_path))