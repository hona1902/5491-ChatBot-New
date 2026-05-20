import os

# ROOT DATA FOLDER
DATA_FOLDER = "./data"

# LANGGRAPH CHECKPOINT FILE
sqlite_folder = f"{DATA_FOLDER}/sqlite-db"
os.makedirs(sqlite_folder, exist_ok=True)
LANGGRAPH_CHECKPOINT_FILE = f"{sqlite_folder}/checkpoints.sqlite"

# UPLOADS FOLDER
UPLOADS_FOLDER = f"{DATA_FOLDER}/uploads"
os.makedirs(UPLOADS_FOLDER, exist_ok=True)

# TIKTOKEN CACHE FOLDER
# Reads TIKTOKEN_CACHE_DIR from the environment so Docker can redirect the cache
# to a path outside /data/ (which is typically volume-mounted and would hide the
# pre-baked encoding baked into the image at build time).
TIKTOKEN_CACHE_DIR = os.environ.get("TIKTOKEN_CACHE_DIR", "").strip() or f"{DATA_FOLDER}/tiktoken-cache"
os.makedirs(TIKTOKEN_CACHE_DIR, exist_ok=True)

# ── Phase 2 Table Safety Controls ────────────────────────────────────────────
# Maximum number of columns kept per extracted table.
# Columns beyond this limit are silently dropped and truncated=True is set.
# Default 100; increase if your data has legitimately wide spreadsheets.
TABLE_MAX_COLS: int = int(os.environ.get("OPEN_NOTEBOOK_TABLE_MAX_COLS", "100"))

# Maximum total characters for the assembled tables_markdown string written to
# source.tables_markdown.  Assembly stops at the last complete table boundary
# before this limit; a truncation comment is appended.
# Default 50 000 (≈50 KB of Markdown) is generous for typical CSV/XLSX files.
TABLES_MARKDOWN_MAX_CHARS: int = int(
    os.environ.get("OPEN_NOTEBOOK_TABLES_MARKDOWN_MAX_CHARS", "50000")
)

# Cosine similarity threshold for the Phase 2 semantic fallback in
# table_exact_lookup (table_lookup.py).  Values below this are treated as
# no-match.  Default 0.4 (mid-range).
TABLE_LOOKUP_SIMILARITY_THRESHOLD: float = float(
    os.environ.get("TABLE_LOOKUP_SIMILARITY_THRESHOLD", "0.4")
)

# Cosine similarity threshold used by ask.py when ranking candidate CSV/XLSX
# sources at the notebook level.  Intentionally low (0.35) to favour recall
# over precision — it is better to include a source than miss it.
ASK_TABLE_SOURCE_THRESHOLD: float = float(
    os.environ.get("ASK_TABLE_SOURCE_THRESHOLD", "0.35")
)

# ── Evidence v2: Full-Content Routing Controls ────────────────────────────
# Maximum number of characters for the full source text injected when
# evidence_need is 'factual' or 'legal_comparison'.
# Default 20 000 (~20 KB).  Content is truncated at the last paragraph
# boundary before this limit; a truncation marker is appended.
EVIDENCE_FULL_TEXT_MAX_CHARS: int = int(
    os.environ.get("EVIDENCE_FULL_TEXT_MAX_CHARS", "20000")
)
