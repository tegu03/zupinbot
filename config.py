"""Central config. Reads everything from environment (.env).

v3 (2 Jul 2026) — sesuai briefing_claude_fable5.md:
  BARU  : bybit_symbol, min_confidence=65, min_rr=2.0, daily_profit_target_pct,
          resume_hour (0 UTC = 07:00 WIB).
  DIPERTAHANKAN dari audit live (bug yang sudah terbukti makan uang):
          min_stop_pct (stop mikro 0.117% -> rugi 2x rencana),
          block_if_position_open, cancel_stale_entries,
          limit_fill_watcher + watch_poll_sec (limit terisi TANPA SL/TP, 2 Jul).
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _f(k, d): return float(os.getenv(k, d))
def _i(k, d): return int(os.getenv(k, d))
def _b(k, d): return os.getenv(k, d).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- DeepSeek (AI) ---
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    thinking: bool = _b("DEEPSEEK_THINKING", "true")

    # --- Lighter (execution + account + market data) ---
    lighter_base_url: str = os.getenv("LIGHTER_BASE_URL", "https://testnet.zklighter.elliot.ai")
    lighter_private_key: str = os.getenv("LIGHTER_PRIVATE_KEY", "")
    lighter_api_key_index: int = _i("LIGHTER_API_KEY_INDEX", "4")
    lighter_account_index: int = _i("LIGHTER_ACCOUNT_INDEX", "139")
    market_index: int = _i("LIGHTER_MARKET_INDEX", "1")          # 1 = BTC-USD perp
    price_decimals: int = _i("LIGHTER_PRICE_DECIMALS", "1")
    size_decimals: int = _i("LIGHTER_SIZE_DECIMALS", "5")
    initial_capital: float = _f("INITIAL_CAPITAL", "1000")
    mkt_slippage: float = _f("MKT_SLIPPAGE", "0.005")
    place_sl_tp: bool = _b("PLACE_SL_TP", "true")
    protect_max_retries: int = _i("PROTECT_MAX_RETRIES", "4")
    protect_retry_backoff_sec: float = _f("PROTECT_RETRY_BACKOFF_SEC", "3")
    guardian_enabled: bool = _b("GUARDIAN_ENABLED", "true")
    guardian_stop_pct: float = _f("GUARDIAN_STOP_PCT", "0.01")
    emergency_close_if_unprotected: bool = _b("EMERGENCY_CLOSE_IF_UNPROTECTED", "true")

    # --- Telegram ---
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # --- Data supplementary (Bybit V5, publik tanpa API key) ---
    bybit_symbol: str = os.getenv("BYBIT_SYMBOL", "BTCUSDT")

    # --- Trading / engine ---
    interval: str = os.getenv("INTERVAL", "15m")
    risk_pct: float = _f("RISK_PCT", "0.01")
    max_leverage: float = _f("MAX_LEVERAGE", "10")
    min_rr: float = _f("MIN_RR", "2.0")                           # naik dari 1.5 (briefing #6)
    min_confidence: float = _f("MIN_CONFIDENCE", "65")            # gerbang keras (briefing #2)
    min_stop_pct: float = _f("MIN_STOP_PCT", "0.0035")            # stop < 0.35% = mikro -> tolak
    daily_loss_limit_pct: float = _f("DAILY_LOSS_LIMIT_PCT", "0.03")
    daily_profit_target_pct: float = _f("DAILY_PROFIT_TARGET_PCT", "0.10")  # profit-LOCK, bukan ramalan
    resume_hour: int = _i("RESUME_HOUR", "0")                     # jam UTC; 0 = 07:00 WIB
    block_if_position_open: bool = _b("BLOCK_IF_POSITION_OPEN", "true")
    cancel_stale_entries: bool = _b("CANCEL_STALE_ENTRIES", "true")
    limit_fill_watcher: bool = _b("LIMIT_FILL_WATCHER", "true")
    watch_poll_sec: float = _f("WATCH_POLL_SEC", "20")
    dry_run: bool = _b("DRY_RUN", "true")
    loop_minutes: int = _i("LOOP_MINUTES", "15")
    notify_every_cycle: bool = _b("NOTIFY_EVERY_CYCLE", "true")
    state_file: str = os.getenv("STATE_FILE", "bot_state.json")


CONFIG = Config()
