from __future__ import annotations

import argparse
from pathlib import Path


def parse_blocks(text: str):
    current_file = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("*** FILE:"):
            current_file = line.split(":", 1)[1].strip()
            index += 1
            continue
        if line == "*** SEARCH":
            index += 1
            search_lines = []
            while index < len(lines) and lines[index] != "*** REPLACE":
                search_lines.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ValueError("missing *** REPLACE")
            index += 1
            replace_lines = []
            while index < len(lines) and not lines[index].startswith("*** SEARCH") and not lines[index].startswith("*** FILE:"):
                replace_lines.append(lines[index])
                index += 1
            if not current_file:
                raise ValueError("SEARCH block before FILE marker")
            yield current_file, "\n".join(search_lines), "\n".join(replace_lines)
            continue
        index += 1


def apply_diff(repo: Path, diff_path: Path) -> list[str]:
    changed = []
    text = diff_path.read_text(encoding="utf-8")
    for rel_path, search, replace in parse_blocks(text):
        target = repo / rel_path
        source = target.read_text(encoding="utf-8")
        if search not in source:
            raise RuntimeError(f"SEARCH block not found in {rel_path} from {diff_path.name}\n--- SEARCH ---\n{search}")
        if source.count(search) != 1:
            raise RuntimeError(f"SEARCH block matched {source.count(search)} times in {rel_path} from {diff_path.name}")
        target.write_text(source.replace(search, replace, 1), encoding="utf-8")
        changed.append(rel_path)
    return changed


def copy_new_files(bundle: Path, repo: Path) -> list[str]:
    copied = []
    new_root = bundle / "new_files"
    for path in sorted(new_root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(new_root)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        copied.append(str(rel))
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply the Agintor oracle/langgraph patch bundle")
    parser.add_argument("repo", type=Path, help="Path to the Agintor repository root")
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent, help="Patch bundle directory")
    parser.add_argument("--new-files-only", action="store_true")
    parser.add_argument("--diffs-only", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    bundle = args.bundle.resolve()
    if not args.diffs_only:
        copied = copy_new_files(bundle, repo)
        print(f"copied {len(copied)} new files")
    if not args.new_files_only:
        changed = []
        for diff_path in sorted((bundle / "existing_edits").glob("*.search_replace.diff")):
            changed.extend(apply_diff(repo, diff_path))
        print(f"applied edits to {len(set(changed))} existing files")


if __name__ == "__main__":
    main()
