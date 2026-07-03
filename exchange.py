"""Lighter integration: account reads + L2-signed order execution, in-process.

v3 — LOGIKA SIGNING/ORDER TIDAK DIUBAH (byte-identik dengan versi teruji live).
Penambahan (semuanya di luar jalur signing):
  + close_all_positions()  : kill switch -> flatten semua posisi (reduce-only IOC)
  + cancel_entry_orders()  : sapu limit entry basi (non reduce-only) tiap siklus
  + open_position()        : position guard (1 posisi maksimum)
  + fill-watcher           : limit entry TIDAK bisa diberi OCO sebelum terisi --
                             exchange MEMBATALKAN order reduce-only saat posisi belum ada
                             (terbukti live 2 Jul 2026: short limit 60.200 terisi TELANJANG).
                             Watcher memasang OCO dalam hitungan detik setelah fill.
  + state latch            : kill/profit latch per hari UTC (reset 00:00 UTC = 07:00 WIB).

SAFETY MODEL:
  1. ENTRY dikirim sekali, tidak pernah di-retry buta (risiko posisi dobel).
  2. Entry MARKET -> OCO SL/TP dipasang langsung (posisi sudah ada).
  3. Entry LIMIT  -> OCO dipasang SAAT FILL oleh watcher; guardian = backstop.
  4. ensure_protection() tiap siklus memproteksi posisi telanjang apa pun.
  5. Jika proteksi benar-benar gagal -> posisi DITUTUP, bukan dibiarkan telanjang.

Order-type facts verified against the official lighter-python examples:
  - SL/TP harus *_LIMIT types (3/5), GTT, expiry sentinel -1.
  - OCO position-tied: BaseAmount=0, saling membatalkan saat salah satu terisi.
  - API nonce manager menghindari drift "invalid signature".
"""
import time
import json
import asyncio
import contextlib
import logging

import lighter
from lighter.signer_client import CreateOrderTxReq
from config import CONFIG

log = logging.getLogger("pte-bot.exchange")

COLLATERAL_KEYS = ["collateral", "available_balance", "available_collateral", "cross_asset_value"]
ACCOUNT_VALUE_KEYS = ["total_asset_value", "account_value", "portfolio_value", "equity"]
POS_SIZE_KEYS = ["position", "position_size", "size", "base_amount"]
POS_ENTRY_KEYS = ["avg_entry_price", "entry_price", "average_entry_price"]
POS_UPNL_KEYS = ["unrealized_pnl", "unrealised_pnl", "uPnl", "upnl"]
POS_SIGN_KEYS = ["sign", "side", "direction"]
POS_MARKET_KEYS = ["market_id", "market_index", "symbol"]


def _pick(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _to_dict(obj):
    for m in ("model_dump", "to_dict", "dict"):
        if hasattr(obj, m):
            with contextlib.suppress(Exception):
                return getattr(obj, m)()
    return obj if isinstance(obj, dict) else {}


# ---- state (baseline harian + latch); hari = tanggal UTC (00:00 UTC = 07:00 WIB) ----
def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_state():
    try:
        with open(CONFIG.state_file) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s):
    with contextlib.suppress(Exception):
        with open(CONFIG.state_file, "w") as f:
            json.dump(s, f)


def _daily_baseline(equity):
    """Equity awal hari ini (reset 00:00 UTC). Hari baru MENGGANTI state -> latch ikut lepas."""
    s = _load_state()
    if s.get("date") != _today():
        s = {"date": _today(), "baseline_equity": equity}
        _save_state(s)
    return float(s.get("baseline_equity", equity))


def kill_latched():
    return bool(_load_state().get("killed_on") == _today())


def latch_kill(daily_pnl_pct):
    s = _load_state()
    s["killed_on"] = _today()
    s["killed_at_pnl_pct"] = daily_pnl_pct
    _save_state(s)


def profit_latched():
    return bool(_load_state().get("profit_on") == _today())


def latch_profit(daily_pnl_pct):
    s = _load_state()
    s["profit_on"] = _today()
    s["profit_at_pnl_pct"] = daily_pnl_pct
    _save_state(s)


def _const(name, fallback):
    return int(getattr(lighter.SignerClient, name, fallback))


OT_LIMIT = _const("ORDER_TYPE_LIMIT", 0)
OT_MARKET = _const("ORDER_TYPE_MARKET", 1)
OT_SL_LIMIT = _const("ORDER_TYPE_STOP_LOSS_LIMIT", 3)
OT_TP_LIMIT = _const("ORDER_TYPE_TAKE_PROFIT_LIMIT", 5)
TIF_IOC = _const("ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", 0)
TIF_GTT = _const("ORDER_TIME_IN_FORCE_GOOD_TILL_TIME", 1)
GTT_EXPIRY = _const("DEFAULT_28_DAY_ORDER_EXPIRY", -1)
GROUP_OCO = _const("GROUPING_TYPE_ONE_CANCELS_THE_OTHER", 2)


def _resp_ok(resp, err):
    if err:
        return False, str(err)
    code = None
    with contextlib.suppress(Exception):
        code = getattr(resp, "code", None)
        if code is None and isinstance(resp, dict):
            code = resp.get("code")
    if code is not None and int(code) not in (0, 200):
        return False, f"code={code}"
    return True, None


class Exchange:
    def __init__(self):
        self.api = None
        self.account_api = None
        self.order_api = None
        self.signer = None
        self._watch_task = None  # fill-watcher untuk limit entry yang resting

    async def start(self):
        self.api = lighter.ApiClient(lighter.Configuration(host=CONFIG.lighter_base_url))
        self.account_api = lighter.AccountApi(self.api)
        self.order_api = lighter.OrderApi(self.api)
        if CONFIG.lighter_private_key:
            self.signer = lighter.SignerClient(
                url=CONFIG.lighter_base_url,
                api_private_keys={CONFIG.lighter_api_key_index: CONFIG.lighter_private_key},
                account_index=CONFIG.lighter_account_index,
                nonce_management_type=lighter.nonce_manager.NonceManagerType.API,
            )
        log.info("exchange ready (signer=%s)", "ON" if self.signer else "OFF read-only")

    async def close(self):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        if self.signer:
            with contextlib.suppress(Exception):
                await self.signer.close()
        if self.api:
            with contextlib.suppress(Exception):
                await self.api.close()

    def _int_price(self, p):
        return int(round(float(p) * (10 ** CONFIG.price_decimals)))

    def _int_size(self, q):
        return int(round(float(q) * (10 ** CONFIG.size_decimals)))

    def _coi(self):
        return int(time.time() * 1000000) % (2 ** 47)

    async def _place(self, coi, size_int, price_int, is_ask, order_type, tif,
                     reduce_only=False, trigger_price=0, expiry=0):
        try:
            tx, resp, err = await self.signer.create_order(
                market_index=CONFIG.market_index,
                client_order_index=coi,
                base_amount=size_int,
                price=price_int,
                is_ask=is_ask,
                order_type=order_type,
                time_in_force=tif,
                reduce_only=reduce_only,
                trigger_price=trigger_price,
                order_expiry=expiry,
            )
            ok, why = _resp_ok(resp, err)
            return {"ok": ok, "tx_hash": str(getattr(resp, "tx_hash", ""))} if ok else {"ok": False, "error": why}
        except TypeError as e:
            return {"ok": False, "error": f"create_order signature mismatch ({e}); check SDK version"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _place_oco(self, sl_trigger, tp_trigger, close_is_ask):
        """SATU tx: TP + SL position-tied yang saling membatalkan. HANYA valid saat
        posisi SUDAH ADA — dikirim sebelum fill akan DIBATALKAN exchange (terbukti live)."""
        buf = CONFIG.mkt_slippage
        is_ask = 1 if close_is_ask else 0

        def limit_for(trigger):
            return trigger * (1 - buf) if close_is_ask else trigger * (1 + buf)

        tp = CreateOrderTxReq(
            MarketIndex=CONFIG.market_index, ClientOrderIndex=self._coi(), BaseAmount=0,
            Price=self._int_price(limit_for(tp_trigger)), IsAsk=is_ask, Type=OT_TP_LIMIT,
            TimeInForce=TIF_GTT, ReduceOnly=1, TriggerPrice=self._int_price(tp_trigger), OrderExpiry=GTT_EXPIRY,
        )
        sl = CreateOrderTxReq(
            MarketIndex=CONFIG.market_index, ClientOrderIndex=self._coi() + 1, BaseAmount=0,
            Price=self._int_price(limit_for(sl_trigger)), IsAsk=is_ask, Type=OT_SL_LIMIT,
            TimeInForce=TIF_GTT, ReduceOnly=1, TriggerPrice=self._int_price(sl_trigger), OrderExpiry=GTT_EXPIRY,
        )
        try:
            tx, resp, err = await self.signer.create_grouped_orders(grouping_type=GROUP_OCO, orders=[tp, sl])
            ok, why = _resp_ok(resp, err)
            return {"ok": ok} if ok else {"ok": False, "error": why}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _protect_with_retry(self, sl_trigger, tp_trigger, close_is_ask, size_int, ref_price):
        last = None
        for attempt in range(1, CONFIG.protect_max_retries + 1):
            res = await self._place_oco(sl_trigger, tp_trigger, close_is_ask)
            if res.get("ok"):
                return {"ok": True, "attempts": attempt}
            last = res.get("error")
            log.warning("protect attempt %d/%d failed: %s", attempt, CONFIG.protect_max_retries, last)
            await asyncio.sleep(CONFIG.protect_retry_backoff_sec)

        out = {"ok": False, "attempts": CONFIG.protect_max_retries, "last_error": last}
        if CONFIG.emergency_close_if_unprotected and size_int > 0:
            log.error("protection failed -> EMERGENCY CLOSE (reduce-only) to avoid naked position")
            out["emergency_close"] = await self._close_market(size_int, close_is_ask, ref_price)
        return out

    async def _close_market(self, size_int, close_is_ask, ref_price):
        px = ref_price * (1 - 0.05) if close_is_ask else ref_price * (1 + 0.05)
        return await self._place(self._coi(), size_int, self._int_price(px), close_is_ask,
                                 OT_MARKET, TIF_IOC, reduce_only=True)

    async def _active_orders(self, market_index):
        auth, err = self.signer.create_auth_token_with_expiry(api_key_index=CONFIG.lighter_api_key_index)
        if err:
            raise RuntimeError(f"auth token: {err}")
        res = await self.order_api.account_active_orders(
            authorization=auth, account_index=CONFIG.lighter_account_index, market_id=int(market_index))
        return (_to_dict(res).get("orders")) or []

    # ---- helpers baru (di luar jalur signing) ----
    @staticmethod
    def open_position(account):
        """Posisi terbuka di market kita (size != 0), else None. Untuk position guard."""
        for p in (account or {}).get("positions", []) or []:
            if str(p.get("market")) != str(CONFIG.market_index):
                continue
            with contextlib.suppress(Exception):
                if abs(float(p.get("size") or 0)) > 0:
                    return p
        return None

    async def cancel_entry_orders(self):
        """Batalkan order NON-reduce-only yang resting (limit entry basi dari siklus lalu).
        Leg proteksi reduce-only TIDAK PERNAH disentuh."""
        if self.signer is None:
            return []
        results = []
        for o in await self._active_orders(CONFIG.market_index):
            od = o if isinstance(o, dict) else _to_dict(o)
            if bool(od.get("reduce_only")):
                continue
            oi = od.get("order_index")
            if oi is None:
                continue
            try:
                _, _, err = await self.signer.cancel_order(CONFIG.market_index, int(oi))
                results.append({"order_index": oi, "ok": not err, "error": str(err) if err else None})
            except Exception as e:
                results.append({"order_index": oi, "ok": False, "error": str(e)})
        return results

    async def close_all_positions(self, account=None):
        """KILL SWITCH: flatten semua posisi di market kita (reduce-only market IOC),
        setelah menyapu limit entry. Mengembalikan hasil per posisi + status flat akhir."""
        out = {"canceled_entries": [], "closed": [], "flat": None}
        if self.signer is None:
            out["flat"] = True
            return out
        with contextlib.suppress(Exception):
            out["canceled_entries"] = await self.cancel_entry_orders()
        acc = account or await self.get_account()
        for pos in acc.get("positions", []):
            mi = pos.get("market")
            if str(mi) != str(CONFIG.market_index):
                out["closed"].append({"market": mi, "ok": False, "skipped": "OTHER_MARKET"})
                continue
            size = 0.0
            with contextlib.suppress(Exception):
                size = abs(float(pos.get("size") or 0))
            entry_px = 0.0
            with contextlib.suppress(Exception):
                entry_px = float(pos.get("entry_price") or 0)
            if size <= 0 or entry_px <= 0:
                out["closed"].append({"market": mi, "ok": False, "error": "size/entry_price tidak terbaca"})
                continue
            close_is_ask = self._position_is_long(pos)  # long ditutup dengan SELL
            res = await self._close_market(self._int_size(size), close_is_ask, entry_px)
            out["closed"].append({"market": mi, **res})
        with contextlib.suppress(Exception):
            await asyncio.sleep(2)
            fresh = await self.get_account()
            out["flat"] = self.open_position(fresh) is None
        return out

    @staticmethod
    def _has_protective(orders):
        for o in orders:
            od = o if isinstance(o, dict) else _to_dict(o)
            ro = od.get("reduce_only")
            trig = od.get("trigger_price")
            with contextlib.suppress(Exception):
                if bool(ro) and float(trig or 0) > 0:
                    return True
        return False

    @staticmethod
    def _position_is_long(pos):
        sign = pos.get("sign")
        if sign is not None:
            return str(sign).strip().lower() in ("1", "long", "buy", "true", "bid", "+")
        with contextlib.suppress(Exception):
            return float(pos.get("size") or 0) > 0
        return True

    async def get_account(self):
        fallback = {
            "base_capital_usd": CONFIG.initial_capital, "equity_usd": CONFIG.initial_capital,
            "available_usd": CONFIG.initial_capital, "unrealized_pnl_usd": 0.0,
            "realized_pnl_today_usd": 0.0, "daily_pnl_pct": 0.0, "positions": [], "source": "fallback",
        }
        try:
            acc_obj = await self.account_api.account(by="index", value=str(CONFIG.lighter_account_index))
            raw = _to_dict(acc_obj)
            node = raw["accounts"][0] if isinstance(raw.get("accounts"), list) and raw["accounts"] else raw

            collateral = float(_pick(node, COLLATERAL_KEYS, 0) or 0)
            positions, u_pnl = [], 0.0
            for p in (node.get("positions") or []):
                pd = _to_dict(p) if not isinstance(p, dict) else p
                size = float(_pick(pd, POS_SIZE_KEYS, 0) or 0)
                if size == 0:
                    continue
                up = float(_pick(pd, POS_UPNL_KEYS, 0) or 0)
                u_pnl += up
                positions.append({
                    "market": _pick(pd, POS_MARKET_KEYS), "size": size,
                    "entry_price": _pick(pd, POS_ENTRY_KEYS), "sign": _pick(pd, POS_SIGN_KEYS),
                    "unrealized_pnl_usd": up,
                })

            equity = _pick(node, ACCOUNT_VALUE_KEYS)
            equity = float(equity) if equity is not None else (collateral + u_pnl)

            baseline = _daily_baseline(equity)
            today_pnl = equity - baseline
            return {
                "base_capital_usd": CONFIG.initial_capital,
                "equity_usd": round(equity, 2),
                "available_usd": round(collateral, 2),
                "unrealized_pnl_usd": round(u_pnl, 2),
                "realized_pnl_today_usd": round(today_pnl, 2),
                "daily_pnl_pct": round((today_pnl / baseline * 100) if baseline else 0.0, 2),
                "total_pnl_usd": round(equity - CONFIG.initial_capital, 2),
                "positions": positions,
                "source": "lighter",
                "_raw": node,
            }
        except Exception as e:
            log.warning("get_account failed, using fallback: %s", e)
            fallback["error"] = f"{type(e).__name__}: {e}"
            return fallback

    async def ensure_protection(self, account):
        actions = []
        if not CONFIG.guardian_enabled or self.signer is None or CONFIG.dry_run:
            return actions
        for pos in account.get("positions", []):
            mi = pos.get("market")
            if mi is None:
                continue
            if str(mi) != str(CONFIG.market_index):
                # Market lain: decimals tidak diketahui; scaling buta = bahaya. Tandai saja.
                actions.append({"market": mi, "status": "SKIPPED_OTHER_MARKET"})
                continue
            try:
                orders = await self._active_orders(mi)
            except Exception as e:
                actions.append({"market": mi, "status": "UNVERIFIED", "detail": str(e)})
                continue
            if self._has_protective(orders):
                continue

            entry_px = 0.0
            with contextlib.suppress(Exception):
                entry_px = float(pos.get("entry_price") or 0)
            if entry_px <= 0:
                actions.append({"market": mi, "status": "NAKED_NO_ENTRY_PRICE"})
                continue

            is_long = self._position_is_long(pos)
            sp = CONFIG.guardian_stop_pct
            if is_long:
                sl, tp, close_is_ask = entry_px * (1 - sp), entry_px * (1 + sp * CONFIG.min_rr), True
            else:
                sl, tp, close_is_ask = entry_px * (1 + sp), entry_px * (1 - sp * CONFIG.min_rr), False
            size_int = self._int_size(abs(float(pos.get("size") or 0)))
            res = await self._protect_with_retry(sl, tp, close_is_ask, size_int, entry_px)
            actions.append({"market": mi, "status": "PROTECTED" if res.get("ok") else "STILL_NAKED", **res})
        return actions

    # ---- FILL WATCHER: pasang SL/TP begitu limit entry yang resting terisi ----
    def start_fill_watcher(self, decision, size_int):
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = asyncio.create_task(self._watch_fill(dict(decision), size_int))

    async def _watch_fill(self, decision, size_int):
        """Poll sampai limit terisi, lalu pasang OCO dalam hitungan detik. Umur dibatasi
        satu periode loop; sapu-basi + guardian tetap backstop. Watcher mati bila proses
        bot mati — setelah restart, guardian menutup celahnya di siklus pertama."""
        deadline = time.time() + CONFIG.loop_minutes * 60
        close_is_ask = decision["side"] != "sell"
        log.info("fill-watcher ON (poll %ss)", CONFIG.watch_poll_sec)
        try:
            while time.time() < deadline:
                await asyncio.sleep(CONFIG.watch_poll_sec)
                try:
                    acc = await self.get_account()
                    pos = self.open_position(acc)
                    if pos:
                        prot = await self._protect_with_retry(
                            decision["stop"], decision["tp1"], close_is_ask, size_int, decision["entry"])
                        await self._watcher_notify(decision, prot)
                        return
                    orders = await self._active_orders(CONFIG.market_index)
                    still = any(not bool((o if isinstance(o, dict) else _to_dict(o)).get("reduce_only"))
                                for o in orders)
                    if not still:
                        log.info("fill-watcher: entry hilang tanpa fill -> stop")
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("fill-watcher poll error: %s", e)
        except asyncio.CancelledError:
            pass

    async def _watcher_notify(self, decision, prot):
        with contextlib.suppress(Exception):
            from notify import send  # lazy import
            if prot.get("ok"):
                msg = (f"🛡️ <b>Limit terisi → SL/TP terpasang</b> (watcher · percobaan {prot.get('attempts')})\n"
                       f"• SL ${decision['stop']:,.1f} · TP ${decision['tp1']:,.1f}")
            elif (prot.get("emergency_close") or {}).get("ok"):
                msg = "🚨 <b>Limit terisi tapi SL/TP GAGAL → posisi ditutup darurat</b> (reduce-only)"
            else:
                msg = ("⚠️ <b>Limit terisi, SL/TP GAGAL, tutup darurat tak terkonfirmasi — CEK POSISI MANUAL!</b>\n"
                       f"• error: {prot.get('last_error')}")
            await send(msg)

    async def execute(self, decision):
        out = {"ok": False, "dry_run": decision["dry_run"], "side": decision["side"],
               "protection": None, "warning": None}

        if decision["dry_run"]:
            out.update({"ok": True, "tx_hash": "DRYRUN-" + str(self._coi()), "note": "dry_run -> no order sent"})
            return out
        if self.signer is None:
            out["error"] = "Signer not initialized (set LIGHTER_PRIVATE_KEY)."
            return out

        size_int = self._int_size(decision["base_amount"])
        if size_int <= 0:
            out["error"] = f"base_amount rounds to 0 at SIZE_DECIMALS={CONFIG.size_decimals}"
            return out

        is_ask = decision["side"] == "sell"
        entry = decision["entry"]
        close_is_ask = not is_ask
        want_protection = bool(CONFIG.place_sl_tp and decision.get("stop") and decision.get("tp1"))

        async def _arm():
            prot = await self._protect_with_retry(decision["stop"], decision["tp1"],
                                                  close_is_ask, size_int, entry)
            out["protection"] = prot
            if not prot.get("ok"):
                if (prot.get("emergency_close") or {}).get("ok"):
                    out["warning"] = "SL/TP could not be placed -> position EMERGENCY-CLOSED (reduce-only)."
                else:
                    out["warning"] = ("SL/TP FAILED and emergency-close did not confirm -- "
                                      "CHECK POSITION MANUALLY: " + str(prot.get("last_error")))

        # ENTRY -- dikirim sekali, tidak pernah di-retry buta.
        if decision["entry_type"] == "market":
            worst = entry * (1 + CONFIG.mkt_slippage) if not is_ask else entry * (1 - CONFIG.mkt_slippage)
            res = await self._place(self._coi(), size_int, self._int_price(worst), is_ask, OT_MARKET, TIF_IOC)
            out.update(res)
            if not res.get("ok"):
                return out
            out["entry_status"] = "filled"
            if want_protection:
                await _arm()
            return out

        # LIMIT entry: OCO TIDAK BISA resting sebelum fill (exchange membatalkannya
        # saat posisi belum ada -- terbukti live 2 Jul 2026). Proteksi diikat ke FILL.
        res = await self._place(self._coi(), size_int, self._int_price(entry), is_ask,
                                OT_LIMIT, TIF_GTT, expiry=GTT_EXPIRY)
        out.update(res)
        if not res.get("ok"):
            return out

        out["entry_status"] = "unknown"
        with contextlib.suppress(Exception):
            await asyncio.sleep(2)
            acc = await self.get_account()
            out["entry_status"] = "filled" if self.open_position(acc) else "resting"

        if out["entry_status"] == "filled":
            if want_protection:
                await _arm()
        elif want_protection:
            if CONFIG.limit_fill_watcher:
                self.start_fill_watcher(decision, size_int)
                out["protection"] = {"deferred": True, "mode": "watcher", "poll_sec": CONFIG.watch_poll_sec}
            else:
                out["protection"] = {"deferred": True, "mode": "guardian-only"}
                out["warning"] = ("Limit resting tanpa watcher: proteksi baru dipasang guardian "
                                  "pada siklus berikutnya setelah fill.")
        return out
