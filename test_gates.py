"""Replay deterministik checklist verifikasi (briefing Section 12) TANPA network/SDK.
Jalankan:  python test_gates.py   -> semua baris harus PASS."""
import os, sys, types, asyncio, json, time

os.environ.update({
    "MIN_CONFIDENCE": "65", "MIN_RR": "2.0", "MIN_STOP_PCT": "0.0035",
    "DAILY_LOSS_LIMIT_PCT": "0.03", "DAILY_PROFIT_TARGET_PCT": "0.10",
    "RESUME_HOUR": "0", "STATE_FILE": "/tmp/zupin_test_state.json",
    "DRY_RUN": "true", "WATCH_POLL_SEC": "0.05", "LOOP_MINUTES": "1",
})
# Stub SDK supaya exchange.py bisa diimpor tanpa lighter terpasang.
_l = types.ModuleType("lighter"); _l.SignerClient = type("SC", (), {})()
_sc = types.ModuleType("lighter.signer_client"); _sc.CreateOrderTxReq = dict
sys.modules.setdefault("lighter", _l); sys.modules.setdefault("lighter.signer_client", _sc)

from risk import evaluate
import exchange as ex

PASS = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS.append(bool(cond)); assert cond, name

def mk(signal, conf, regime, entry=60000, stop=59500, tp1=61200, etype="market"):
    pte = {"signal": signal, "confidence_pct": conf, "regime": regime,
           "entry": {"type": etype, "price": entry}, "invalidation": stop,
           "targets": [tp1, None], "abstain_reason": "", "flip_if": ""}
    mse = {"pte_layer1_input": regime}
    snap = {"account": {"equity_usd": 9300.0, "daily_pnl_pct": 0.0}}
    return pte, mse, snap

def why(d): return " | ".join(d["reasons"])

# --- Confidence gate (checklist: 40 & 60 ditolak; 65 & 80 lolos) ---
d = evaluate(*mk("long", 40, "trending_up"));  check("conf 40 -> DITOLAK", not d["approved"] and "Confidence" in why(d))
d = evaluate(*mk("long", 60, "trending_up"));  check("conf 60 -> DITOLAK", not d["approved"])
d = evaluate(*mk("long", 65, "trending_up"));  check("conf 65 + gate lain lolos -> APPROVED", d["approved"])
d = evaluate(*mk("short", 80, "trending_down", 60000, 60600, 58600)); check("conf 80 short/down -> APPROVED", d["approved"])

# --- Regime alignment ---
d = evaluate(*mk("short", 80, "trending_up", 60000, 60600, 58600)); check("trending_up + SHORT -> DITOLAK", not d["approved"])
d = evaluate(*mk("long", 80, "trending_down"));                     check("trending_down + LONG -> DITOLAK", not d["approved"])
d = evaluate(*mk("long", 80, "ranging"));                           check("ranging -> DITOLAK", not d["approved"])
d = evaluate(*mk("short", 80, "chop", 60000, 60600, 58600));        check("chop -> DITOLAK", not d["approved"])

# --- R:R & stop mikro ---
d = evaluate(*mk("long", 70, "trending_up", 60000, 59500, 60900));  check("R:R 1.8 < 2.0 -> DITOLAK", not d["approved"])
d = evaluate(*mk("long", 70, "trending_up", 60000, 59880, 60300));  check("stop 0.2% (mikro) -> DITOLAK", not d["approved"] and "mikro" in why(d))

# --- Kill switch & profit lock (flag dari governor) ---
p, m, s = mk("long", 80, "trending_up"); s["account"]["daily_pnl_pct"] = -3.1
d = evaluate(p, m, s); check("daily -3.1% -> kill_switch=True, DITOLAK", d["kill_switch"] and not d["approved"])
p, m, s = mk("long", 80, "trending_up"); s["account"]["daily_pnl_pct"] = -2.0
d = evaluate(p, m, s); check("daily -2.0% -> masih boleh trade", d["approved"] and not d["kill_switch"])
p, m, s = mk("no_trade", 0, "chop", None, None, None); s["account"]["daily_pnl_pct"] = 10.5
d = evaluate(p, m, s); check("daily +10.5% -> profit_lock=True", d["profit_lock"])

# --- Latch state (kill terkunci hari ini, lepas saat hari UTC berganti) ---
with open("/tmp/zupin_test_state.json", "w") as f:
    json.dump({"date": ex._today(), "baseline_equity": 10000.0}, f)
ex.latch_kill(-3.5); check("latch_kill -> kill_latched True", ex.kill_latched())
with open("/tmp/zupin_test_state.json", "w") as f:  # simulasi kemarin
    json.dump({"date": "2000-01-01", "baseline_equity": 10000.0, "killed_on": "2000-01-01"}, f)
ex._daily_baseline(9000.0); check("hari baru -> latch lepas + baseline reset", not ex.kill_latched())
ex.latch_profit(10.2); check("latch_profit -> profit_latched True", ex.profit_latched())

# --- Resume-time calc (0 < detik <= 24 jam) ---
import main as mn
secs = mn._seconds_until_resume(); check(f"seconds_until_resume dalam rentang (={secs})", 0 < secs <= 86460)

# --- Fill-watcher: OCO dipasang saat limit terisi (poll ke-3) ---
class Fake(ex.Exchange):
    def __init__(self):
        self._watch_task = None; self.polls = 0; self.protected = None; self.notified = False
    async def get_account(self):
        self.polls += 1
        pos = [{"market": str(ex.CONFIG.market_index), "size": 0.3, "entry_price": 60200, "sign": "short"}]
        return {"positions": pos if self.polls >= 3 else []}
    async def _active_orders(self, mi): return [{"reduce_only": False, "order_index": 7}]
    async def _protect_with_retry(self, sl, tp, cia, size_int, ref):
        self.protected = (sl, tp, cia, size_int); return {"ok": True, "attempts": 1}
    async def _watcher_notify(self, d, p): self.notified = True

f = Fake()
asyncio.run(f._watch_fill({"side": "sell", "stop": 60500.0, "tp1": 59600.0, "entry": 60200.0}, 31296))
check("watcher: fill poll-3 -> OCO dipasang (SL 60500/TP 59600, close BUY)",
      f.protected == (60500.0, 59600.0, False, 31296) and f.polls >= 3 and f.notified)

# --- Watcher berhenti bila entry hilang tanpa fill ---
class Fake2(Fake):
    async def get_account(self): self.polls += 1; return {"positions": []}
    async def _active_orders(self, mi): return []
f2 = Fake2(); t0 = time.time()
asyncio.run(f2._watch_fill({"side": "buy", "stop": 59000.0, "tp1": 61000.0, "entry": 60000.0}, 1000))
check("watcher: entry dibatalkan -> berhenti cepat", f2.protected is None and time.time() - t0 < 5)

print(f"\n{sum(PASS)}/{len(PASS)} PASS — semua gerbang sesuai checklist briefing")
