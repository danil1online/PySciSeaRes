import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "vak.db")
AUTOREFS_DIR = os.path.join(BASE_DIR, "autorefs")
LLM_RESPONSES_DIR = os.path.join(BASE_DIR, "llm_responses")
SCI_SPEC_FILE = os.path.join(BASE_DIR, "sci_spec.txt")

API_BASE = "https://vak.gisnauka.ru/api"
LLM_API_URL = "http://195.133.13.56:8080/v1/chat/completions"
LLM_MODEL = "Qwen3.5-2B-Q4_K_M.gguf"

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
MIN_SIZE = 300 * 1024

MAX_PUBLICATIONS = 30
SYNC_DAYS = 30
RESULTS_PER_PAGE = 1000
MAX_SPECIALTIES = 999999

SESSION_SECRET_KEY = os.environ.get("VAK_SECRET_KEY", "vak-secret-key-change-me")
PDF_TIMEOUT = 120
LLM_TIMEOUT = 120
LLM_MAX_TOKENS = 2000
LLM_TEMPERATURE = 0.1
