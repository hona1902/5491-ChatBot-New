#!/usr/bin/env python3
"""
Backfill source_table records for existing sources.

Processes Source records that pre-date the table-aware QA ingestion pipeline
and extracts/persists their structured table data.

Usage:
    python scripts/backfill_tables.py [options]

    --source-ids source:abc,source:def   Comma-separated source IDs to process
    --force           Replace existing source_table records (idempotent)
    --dry-run         Report what would happen without writing to DB
    --skip-embeddings Skip re-embedding table row chunks (prints a visible warning)
    --concurrency N   Number of concurrent sources to process (default: 1)

Examples:
    # Dry-run on all sources
    python scripts/backfill_tables.py --dry-run

    # Process specific sources
    python scripts/backfill_tables.py --source-ids source:abc,source:def

    # Force-replace existing table records for all sources
    python scripts/backfill_tables.py --force

    # Skip embeddings (not recommended for production)
    python scripts/backfill_tables.py --skip-embeddings
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on PYTHONPATH when run directly
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from loguru import logger

from open_notebook.config import TABLES_MARKDOWN_MAX_CHARS
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import Source, SourceTable
from open_notebook.utils.table_extractor_registry import extract_tables_from_source


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill source_table records for existing sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source-ids",
        type=str,
        default=None,
        metavar="IDS",
        help=(
            "Comma-separated source IDs to process "
            "(e.g. source:abc,source:def). "
            "Omit to process all sources."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Replace existing source_table records. "
            "Default: skip sources that already have table records."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be processed without writing to the database.",
    )
    parser.add_argument(
        "--skip-embeddings",
        action="store_true",
        default=False,
        help=(
            "Do not re-embed table row chunks after backfill. "
            "A visible WARNING is printed at the end of the run."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="Number of concurrent sources to process (default: 1).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------

async def _load_sources(source_ids: Optional[List[str]]) -> List[Source]:
    """Load Source records from DB.

    If *source_ids* is given, load only those IDs (unknown IDs are warned and
    skipped).  Otherwise load all sources.
    """
    if source_ids:
        sources: List[Source] = []
        for sid in source_ids:
            try:
                src = await Source.get(sid)
                if src:
                    sources.append(src)
                else:
                    logger.warning(f"Backfill: source not found: {sid!r} — skipping")
            except Exception as exc:
                logger.warning(f"Backfill: could not load source {sid!r}: {exc} — skipping")
        return sources

    # Load all sources (omit large full_text for speed)
    try:
        rows = await repo_query(
            "SELECT * OMIT full_text, tables_markdown FROM source"
        )
        return [Source(**row) for row in rows] if rows else []
    except Exception as exc:
        logger.error(f"Backfill: failed to load all sources: {exc}")
        raise


# ---------------------------------------------------------------------------
# tables_markdown assembly (mirrors source.py logic)
# ---------------------------------------------------------------------------

def _assemble_tables_markdown(markdown_parts: List[str], source_id: str) -> Optional[str]:
    """Assemble tables_markdown from per-table markdown parts with a char cap.

    Mirrors the assembly logic in open_notebook/graphs/source.py so that
    backfilled sources have identical tables_markdown to freshly ingested ones.
    """
    if not markdown_parts:
        return None

    assembled: List[str] = []
    running_chars = 0
    separator = "\n\n"
    sep_len = len(separator)
    omitted = 0

    for part in markdown_parts:
        candidate_addition = (sep_len if assembled else 0) + len(part)
        if running_chars + candidate_addition > TABLES_MARKDOWN_MAX_CHARS:
            omitted = len(markdown_parts) - len(assembled)
            logger.warning(
                f"Backfill: tables_markdown cap ({TABLES_MARKDOWN_MAX_CHARS} chars) "
                f"reached for source '{source_id}'; {omitted} table(s) omitted from markdown"
            )
            break
        assembled.append(part)
        running_chars += candidate_addition

    if not assembled:
        return None

    result = separator.join(assembled)
    if omitted > 0:
        result += (
            f"\n\n<!-- tables_markdown truncated: {omitted} table(s) omitted "
            f"(OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS={TABLES_MARKDOWN_MAX_CHARS}) -->"
        )
    return result


# ---------------------------------------------------------------------------
# Per-source processing
# ---------------------------------------------------------------------------

async def _process_source(
    source: Source,
    *,
    force: bool,
    dry_run: bool,
    skip_embeddings: bool,
) -> Dict[str, Any]:
    """Process a single source.

    Returns a result dict with keys:
        status      "OK" | "SKIP" | "ERROR"
        source_id   str
        table_count int
        row_count   int
        reason      Optional[str]   (human-readable explanation for SKIP/ERROR)
    """
    source_id = str(source.id)
    base: Dict[str, Any] = {
        "source_id": source_id,
        "table_count": 0,
        "row_count": 0,
        "reason": None,
    }

    # 1. Idempotency check — skip sources with existing records unless --force
    try:
        existing_tables = await SourceTable.get_for_source(source_id)
    except Exception as exc:
        return {**base, "status": "ERROR", "reason": f"DB error checking existing tables: {exc}"}

    if existing_tables and not force:
        return {
            **base,
            "status": "SKIP",
            "reason": (
                f"already has {len(existing_tables)} source_table record(s); "
                "use --force to replace"
            ),
        }

    # 2. Locate file path
    file_path: Optional[str] = None
    if source.asset and source.asset.file_path:
        file_path = source.asset.file_path

    if not file_path:
        return {
            **base,
            "status": "SKIP",
            "reason": "no file_path (URL-only source or file already deleted)",
        }

    if not Path(file_path).exists():
        logger.warning(
            f"Backfill: file not found for source {source_id!r}: {file_path!r} — skipping"
        )
        return {
            **base,
            "status": "SKIP",
            "reason": f"file not found: {file_path!r}",
        }

    # 3. Run table extractor (sync dispatcher; run in thread executor)
    try:
        loop = asyncio.get_event_loop()
        tables = await loop.run_in_executor(
            None, extract_tables_from_source, file_path, source_id
        )
    except Exception as exc:
        logger.error(f"Backfill: extraction failed for source {source_id!r}: {exc}")
        return {**base, "status": "ERROR", "reason": f"extraction error: {exc}"}

    # 4. Dry-run: report without writing
    if dry_run:
        total_rows = sum(t.row_count for t in tables)
        return {
            **base,
            "status": "OK",
            "table_count": len(tables),
            "row_count": total_rows,
            "reason": "dry-run; no writes performed",
        }

    # 5. --force: delete existing source_table records (and their embeddings) first
    if force and existing_tables:
        try:
            await repo_query(
                "DELETE source_table WHERE source = $source_id",
                {"source_id": ensure_record_id(source_id)},
            )
            # Remove table-row embeddings tagged to this source so they don't
            # orphan if --skip-embeddings is not used.
            await repo_query(
                "DELETE source_embedding WHERE source = $source_id AND chunk_type = 'table_row'",
                {"source_id": ensure_record_id(source_id)},
            )
        except Exception as exc:
            logger.warning(
                f"Backfill: failed to delete existing records for {source_id!r}: {exc} — "
                "continuing with re-extraction"
            )

    # 6. Persist source_table records
    markdown_parts: List[str] = []
    saved_count = 0
    total_rows = 0

    for table in tables:
        try:
            st = SourceTable(
                source=str(ensure_record_id(source_id)),
                table_id=table.table_id,
                page_number=table.page_number,
                sheet_name=table.sheet_name,
                column_headers=table.column_headers,
                original_headers=table.original_headers,
                row_data=table.row_data,
                markdown_repr=table.markdown_repr,
                row_count=table.row_count,
                col_count=table.col_count,
                truncated=table.truncated,
            )
            await st.save()
            saved_count += 1
            total_rows += table.row_count

            label_parts = [f"Table: {table.table_id}"]
            if table.page_number is not None:
                label_parts.append(f"Page {table.page_number}")
            if table.sheet_name:
                label_parts.append(f"Sheet: {table.sheet_name}")
            if table.truncated:
                label_parts.append("[truncated]")
            markdown_parts.append(
                f"<!-- {', '.join(label_parts)} -->\n{table.markdown_repr}"
            )
        except Exception as exc:
            logger.warning(
                f"Backfill: failed to save SourceTable "
                f"for table_id='{table.table_id}': {exc}"
            )

    # 7. Update source.tables_markdown
    tables_markdown = _assemble_tables_markdown(markdown_parts, source_id)
    if tables_markdown:
        try:
            source.tables_markdown = tables_markdown
            await source.save()
        except Exception as exc:
            logger.warning(
                f"Backfill: failed to update tables_markdown for {source_id!r}: {exc}"
            )

    return {
        **base,
        "status": "OK",
        "table_count": saved_count,
        "row_count": total_rows,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main() -> int:
    args = _parse_args()

    # Parse comma-separated source IDs
    source_ids: Optional[List[str]] = None
    if args.source_ids:
        source_ids = [s.strip() for s in args.source_ids.split(",") if s.strip()]

    mode_label = "[DRY-RUN]" if args.dry_run else "[WRITE]"
    print(f"\nBackfill table records — mode: {mode_label}")
    if args.force and not args.dry_run:
        print("  --force:           existing source_table records will be replaced")
    if args.skip_embeddings:
        print("  --skip-embeddings: table row embeddings will NOT be refreshed")
    if source_ids:
        print(f"  --source-ids:      {source_ids}")
    print()

    # Load sources
    try:
        sources = await _load_sources(source_ids)
    except Exception as exc:
        logger.error(f"Backfill: aborting — could not load sources: {exc}")
        return 1

    if not sources:
        print("No sources to process.")
        return 0

    print(f"Processing {len(sources)} source(s)...\n")

    # Process with bounded concurrency
    semaphore = asyncio.Semaphore(args.concurrency)

    async def _bounded(src: Source) -> Dict[str, Any]:
        async with semaphore:
            return await _process_source(
                src,
                force=args.force,
                dry_run=args.dry_run,
                skip_embeddings=args.skip_embeddings,
            )

    tasks = [_bounded(src) for src in sources]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: List[Dict[str, Any]] = []
    for i, r in enumerate(raw):
        if isinstance(r, Exception):
            r = {
                "status": "ERROR",
                "source_id": str(sources[i].id),
                "table_count": 0,
                "row_count": 0,
                "reason": str(r),
            }
        results.append(r)
        status = r["status"]
        sid = r["source_id"]
        t_count = r["table_count"]
        row_count = r["row_count"]
        reason = r.get("reason") or ""
        reason_str = f"  ({reason})" if reason else ""
        print(f"  {status:<5}  {sid}  tables={t_count}  rows={row_count}{reason_str}")

    # Summary
    ok = sum(1 for r in results if r["status"] == "OK")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    failed = sum(1 for r in results if r["status"] == "ERROR")
    total_tables = sum(r["table_count"] for r in results)
    total_rows = sum(r["row_count"] for r in results)

    print(f"\n{'─' * 60}")
    print(f"  Mode:     {mode_label}")
    print(f"  Scanned:  {len(sources)}")
    print(f"  Updated:  {ok}  (tables={total_tables}, rows={total_rows})")
    print(f"  Skipped:  {skipped}")
    print(f"  Failed:   {failed}")
    print(f"{'─' * 60}\n")

    if args.skip_embeddings:
        print(
            "WARNING: --skip-embeddings was set. Semantic row retrieval will not work "
            "for backfilled sources until embeddings are generated. "
            "Run again without --skip-embeddings to complete.\n"
        )

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
