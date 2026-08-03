"""Push chapters built by the pdf/ pipeline into Supabase.

The pipeline produces build/<book_id>_chNN/{chapter.json, assets/}. This reads
those directories and writes them through app/lib/textbook_persist.py -- the
same module the admin upload route uses, so a chapter loaded from the command
line is indistinguishable from one ingested through the portal.

    # see what would happen, touch nothing
    python -m scripts.publish_textbook --school <school_id> \
        --build ../../pdf/build --dry-run

    # load a whole book
    python -m scripts.publish_textbook --school <school_id> \
        --build ../../pdf/build

    # one chapter, forced past the unchanged check
    python -m scripts.publish_textbook --school <school_id> \
        --build ../../pdf/build --only 3 --force

Chapters land unpublished. Review the split in the admin portal and publish
there; nothing is servable until someone has looked at it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.lib.supabase_clients import create_admin_client  # noqa: E402
from app.lib.textbook_persist import publish_book  # noqa: E402


def chapter_dirs(build_root: Path, book: str | None, only: set[int] | None) -> list[Path]:
    found = sorted(p.parent for p in build_root.glob("*/chapter.json"))
    if book:
        found = [d for d in found if d.name.startswith(book)]
    if only:
        kept = []
        for directory in found:
            meta = json.loads((directory / "chapter.json").read_text(encoding="utf-8"))
            if meta["metadata"]["chapter_number"] in only:
                kept.append(directory)
        found = kept
    return found


def describe(chapter_dir: Path) -> str:
    meta = json.loads((chapter_dir / "chapter.json").read_text(encoding="utf-8"))["metadata"]
    return (
        f"  ch{meta['chapter_number']:02d}  p{meta['source_pages'][0]}-{meta['source_pages'][1]}"
        f"  {meta['subject']} / {meta['language']}  {meta['chapter_title'][:44]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish built textbook chapters to Supabase.")
    parser.add_argument("--school", required=True, help="school_id these chapters belong to.")
    parser.add_argument(
        "--build", type=Path, required=True, help="The pipeline's build/ directory."
    )
    parser.add_argument("--book", help="Only directories whose name starts with this book_id.")
    parser.add_argument("--only", help="Only these chapter numbers, e.g. '1,4,7'.")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-upload even when content_sha256 says the chapter is unchanged.",
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="Mark the chapters servable immediately, skipping admin review.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be published and stop."
    )
    args = parser.parse_args()

    if not args.build.is_dir():
        raise SystemExit(f"No such build directory: {args.build}")

    only = {int(n) for n in args.only.replace(" ", "").split(",") if n} if args.only else None
    targets = chapter_dirs(args.build, args.book, only)
    if not targets:
        raise SystemExit(f"No chapter.json found under {args.build}")

    print(f"{len(targets)} chapter(s) in {args.build}, for school {args.school}:")
    for directory in targets:
        print(describe(directory))

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    if args.publish:
        print("\n--publish: these will be servable immediately, without review.")

    print()
    summary = publish_book(
        create_admin_client(), args.school, targets,
        force=args.force, publish=args.publish, progress=lambda line: print(f"  {line}"),
    )

    actions = [p["action"] for p in summary["published"]]
    print(
        f"\n{actions.count('created')} created, {actions.count('updated')} updated, "
        f"{actions.count('unchanged')} unchanged"
        f"  |  {summary['topics']} topic rows, {summary['images']} images"
    )
    if summary["failed"]:
        print(f"\n{len(summary['failed'])} chapter(s) failed:")
        for failure in summary["failed"]:
            print(f"  {failure['chapter_dir']}: {failure['error']}")
        raise SystemExit(1)
    if not args.publish:
        print("\nChapters are unpublished. Review and publish them in the admin portal.")


if __name__ == "__main__":
    main()
