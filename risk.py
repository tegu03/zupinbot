"""DETERMINISTIC Risk Governor v3. AI mengusulkan; kode ini yang memutuskan.
Menghitung ulang R:R dari nol, sizing dari STOP (fixed fractional), dan menegakkan
gerbang keras. Tidak ada yang mempercayai matematika model.

Gerbang (urutan evaluasi — SEMUA harus lolos):
  1. Sinyal actionable (long/short)
  2. Regime-signal alignment : trending_up->long saja, trending_down->short saja,
                               ranging/chop/unknown -> NO-TRADE (kebijakan trend-only)
  3. Confidence >= MIN_CONFIDENCE (65)          [Bug #2]
  4. Geometry valid + R:R >= MIN_RR (2.0)       [Bug #6]
  5. Stop >= MIN_STOP_PCT (0.35%)               [audit live: stop mikro 0.117% -> rugi 2x rencana]
  6. Sizing dari stop, notional <= max_leverage x equity
  7. Kill switch  : daily <= -3%  -> approved=False + kill_switch=True   [Bug #4]
  8. Profit lock  : daily >= +target -> approved=False + profit_lock=True
"""
from config import CONFIG


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def evaluate(pte, mse, snapshot):
    cfg = CONFIG
    acc = snapshot.get("account", {})
    equity = _num(acc.get("equity_usd")) or cfg.initial_capital
    reasons, approved = [], True
    kill_switch = False
    profit_lock = False

    signal = pte.get("signal")
    if signal not in ("long", "short"):
        approved = False
        reasons.append(f"Signal not actionable: {signal}")

    # --- REGIME ALIGNMENT (MSE layer1 = otoritatif; PTE hanya cross-check) ---
    regime = mse.get("pte_layer1_input") or pte.get("regime")
    pte_regime = pte.get("regime")
    if pte_regime and regime and pte_regime != regime:
        reasons.append(f"Catatan: regime PTE ({pte_regime}) != MSE ({regime}); dipakai MSE")
    if signal in ("long", "short"):
        if regime == "trending_up" and signal != "long":
            approved = False
            reasons.append("Sinyal SHORT berlawanan regime trending_up -> DITOLAK")
        elif regime == "trending_down" and signal != "short":
            approved = False
            reasons.append("Sinyal LONG berlawanan regime trending_down -> DITOLAK")
        elif regime not in ("trending_up", "trending_down"):
            approved = False
            reasons.append(f"Regime {regime} -> NO-TRADE (kebijakan trend-only)")

    # --- CONFIDENCE GATE (hanya untuk sinyal arah) ---
    conf = _num(pte.get("confidence_pct"))
    if signal in ("long", "short"):
        if conf is None or conf < cfg.min_confidence:
            approved = False
            reasons.append(f"Confidence {conf if conf is not None else 0:.0f}% < minimum {cfg.min_confidence:.0f}%")

    entry_obj = pte.get("entry") or {}
    entry = _num(entry_obj.get("price"))
    if entry is None:
        zone = entry_obj.get("zone") or [None]
        entry = _num(zone[0])
    stop = _num(pte.get("invalidation"))
    targets = pte.get("targets") or []
    tp1 = _num(targets[0]) if len(targets) > 0 else None
    tp2 = _num(targets[1]) if len(targets) > 1 else None

    if signal in ("long", "short") and (entry is None or stop is None):
        approved = False
        reasons.append("Missing entry or invalidation")

    rr = stop_dist = risk_usd = notional = base_amount = side = None
    if entry is not None and stop is not None and tp1 is not None:
        risk_dist = abs(entry - stop)
        reward_dist = abs(tp1 - entry)
        rr = reward_dist / risk_dist if risk_dist > 0 else 0
        if signal == "long" and not (stop < entry < tp1):
            approved = False
            reasons.append("Long geometry invalid (need stop<entry<tp1)")
        if signal == "short" and not (stop > entry > tp1):
            approved = False
            reasons.append("Short geometry invalid (need stop>entry>tp1)")
        if rr < cfg.min_rr:
            approved = False
            reasons.append(f"R:R {rr:.2f} < min {cfg.min_rr}")
        stop_dist = risk_dist / entry if entry > 0 else 0
        # Stop mikro di dalam noise floor: slippage memakan stop; realized risk >> rencana.
        if stop_dist < cfg.min_stop_pct:
            approved = False
            reasons.append(f"Stop {stop_dist * 100:.3f}% < minimum {cfg.min_stop_pct * 100:.2f}% (stop mikro)")
        risk_usd = equity * cfg.risk_pct
        notional = risk_usd / stop_dist if stop_dist > 0 else 0
        cap = equity * cfg.max_leverage
        if notional > cap:
            notional = cap
            reasons.append(f"Notional capped at {cfg.max_leverage}x equity")
        base_amount = notional / entry if entry > 0 else 0
        side = "buy" if signal == "long" else "sell"
    elif signal in ("long", "short"):
        approved = False
        reasons.append("Missing TP1 for R:R / sizing")

    ev = str(pte.get("event_risk") or "")
    if ev and any(w in ev.lower() for w in ("high-impact", "imminent", "within hours", "fomc", "cpi", "nfp", "expiry")):
        reasons.append(f"Event risk noted: {ev}")

    # --- KILL SWITCH: rugi harian ---
    dp = _num(acc.get("daily_pnl_pct"))
    if dp is not None and dp <= -(cfg.daily_loss_limit_pct * 100):
        approved = False
        kill_switch = True
        reasons.append(f"KILL SWITCH: daily {dp:.2f}% <= -{cfg.daily_loss_limit_pct * 100:.1f}%")

    # --- PROFIT LOCK: kunci hari yang sudah hijau (disiplin, bukan ramalan) ---
    if dp is not None and cfg.daily_profit_target_pct > 0 and dp >= cfg.daily_profit_target_pct * 100:
        approved = False
        profit_lock = True
        reasons.append(f"PROFIT LOCK: daily +{dp:.2f}% >= target {cfg.daily_profit_target_pct * 100:.1f}% -> stop entry hari ini")

    if not reasons:
        reasons.append("All gates passed")

    return {
        "approved": bool(approved and signal in ("long", "short")),
        "kill_switch": kill_switch,
        "profit_lock": profit_lock,
        "signal": signal,
        "side": side,
        "regime": regime,
        "confidence_pct": pte.get("confidence_pct"),
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "entry_type": entry_obj.get("type") or "limit",
        "rr": round(rr, 2) if rr is not None else None,
        "stop_distance_pct": round(stop_dist * 100, 3) if stop_dist is not None else None,
        "risk_usd": round(risk_usd, 2) if risk_usd is not None else None,
        "notional_usd": round(notional, 2) if notional is not None else None,
        "base_amount": round(base_amount, 6) if base_amount is not None else None,
        "equity_usd": round(equity, 2),
        "market_index": cfg.market_index,
        "dry_run": cfg.dry_run,
        "reasons": reasons,
        "abstain_reason": pte.get("abstain_reason") or "",
        "flip_if": pte.get("flip_if") or "",
        "counter_thesis": pte.get("counter_thesis") or "",
        "funding_note": pte.get("funding_note") or "",
    }
