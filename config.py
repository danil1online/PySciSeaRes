import os

# ======================== PROJECT PATHS ========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "vak.db")
AUTOREFS_DIR = os.path.join(BASE_DIR, "autorefs")
LLM_RESPONSES_DIR = os.path.join(BASE_DIR, "llm_responses")
SCI_SPEC_FILE = os.path.join(BASE_DIR, "sci_spec.txt")
CLUSTER_CSV_PATH = os.path.join(BASE_DIR, "dep-clust-new.csv")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ======================== API ENDPOINTS ========================
API_BASE = "https://vak.gisnauka.ru/api"
LLM_API_URL = os.environ.get(
    "LLM_API_URL",
    "http://195.133.13.56:1234/v1/chat/completions"
)
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5-4b")

# Local llama.cpp server (for memory management experiments)
LLM_LOCAL_URL = os.environ.get("LLM_LOCAL_URL", "http://localhost:8080/v1/chat/completions")
LLM_LOCAL_MODEL = os.environ.get("LLM_LOCAL_MODEL", "qwen3.5-2b")

# ======================== HTTP HEADERS ========================
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

# ======================== DOWNLOAD / PDF ========================
MIN_SIZE = 200 * 1024  # 200 KB
PDF_TIMEOUT = 120

# ======================== PUBLICATIONS ========================
MAX_PUBLICATIONS = 30

# ======================== SYNC ========================
SYNC_DAYS = 30
MAX_SPECIALTIES = 999999

# ======================== SEARCH ========================
RESULTS_PER_PAGE = 1000

# ======================== SESSION ========================
SESSION_SECRET_KEY = os.environ.get("VAK_SECRET_KEY", "vak-secret-key-change-me")

# ======================== LLM ========================
LLM_TIMEOUT = 120
LLM_JSON_TIMEOUT = 300  # cold start for JSON-LLM
LLM_MAX_TOKENS = 2000
LLM_TEMPERATURE = 0.1

# Cache
VAK_CACHE_ENABLED = os.environ.get("VAK_CACHE_ENABLED", "1") == "1"
CACHE_TTL_DAYS = 7

# ======================== RATE LIMITING ========================
# VAK API: max requests per minute
VAK_API_MAX_REQUESTS_PER_MINUTE = 30

# External sources: max requests per minute
DDG_MAX_REQUESTS_PER_MINUTE = 10
SEMANTIC_SCHOLAR_MAX_REQUESTS_PER_MINUTE = 20
CROSSREF_MAX_REQUESTS_PER_MINUTE = 15
DOI_RESOLVER_MAX_REQUESTS_PER_MINUTE = 20

# ======================== MEMORY MANAGEMENT ========================
# For 16GB RAM system with ~7GB used by other services + LLM
# Soft limit: trigger GC at 10GB process memory
# Critical limit: stop at 12GB to avoid OOM killer
MAX_MEMORY_MB = 10 * 1024  # 10 GB soft limit
CRITICAL_MEMORY_MB = 12 * 1024  # 12 GB critical limit
BATCH_SIZE = 10  # GC check every N adverts

# ======================== ADMIN ========================
DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
