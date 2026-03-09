from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from .exceptions import PatchApplyError
from .utils import unique_search_match


PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\n(?P<search>.*?)\n=======\n(?P<replace>.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)


@dataclass
class SearchReplaceBlock:
    search: str
    replace: str



def parse_patch(text: str) -> list[SearchReplaceBlock]:
    blocks = [SearchReplaceBlock(search=m.group("search"), replace=m.group("replace")) for m in PATCH_RE.finditer(text)]
    if not blocks:
        raise PatchApplyError("patch contained no SEARCH/REPLACE blocks")
    return blocks



def apply_patch_to_text(source: str, patch_text: str) -> str:
    result = source
    blocks = parse_patch(patch_text)
    for block in blocks:
        match_status = unique_search_match(result, block.search)
        if match_status == -1:
            raise PatchApplyError("SEARCH block not found")
        if match_status == -2:
            raise PatchApplyError("SEARCH block matched multiple locations")
        result = result.replace(block.search, block.replace, 1)
    return result



def apply_patch_to_file(path: Path, patch_text: str) -> None:
    source = path.read_text(encoding="utf-8")
    updated = apply_patch_to_text(source, patch_text)
    path.write_text(updated, encoding="utf-8")



def build_patch(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"
