"""One-off helper. Run:  python find_account.py 0xYOURWALLET
Prints your account index (put into LIGHTER_ACCOUNT_INDEX) and dumps a few markets'
details so you can read price/size decimals + confirm the BTC market index."""
import asyncio
import sys
import json

import lighter
from config import CONFIG


def _d(o):
    for m in ("model_dump", "to_dict", "dict"):
        if hasattr(o, m):
            try:
                return getattr(o, m)()
            except Exception:
                pass
    return o if isinstance(o, dict) else str(o)


async def main(addr):
    api = lighter.ApiClient(lighter.Configuration(host=CONFIG.lighter_base_url))
    try:
        if addr:
            try:
                r = await lighter.AccountApi(api).accounts_by_l1_address(l1_address=addr)
                d = _d(r)
                subs = d.get("sub_accounts") or d.get("accounts") or []
                idx = [(_d(s).get("index") if isinstance(_d(s), dict) else s) for s in subs]
                print("\n=== ACCOUNT INDEXES for", addr, "===")
                print(idx or d)
            except Exception as e:
                print("account lookup failed:", e)

        print("\n=== MARKET DETAILS (find BTC + read decimals) ===")
        order_api = lighter.OrderApi(api)
        for mi in range(0, 6):
            try:
                fn = getattr(order_api, "order_book_details", None) or getattr(order_api, "order_book_detail")
                res = await fn(market_id=mi)
                print(f"\n--- market_index {mi} ---")
                print(json.dumps(_d(res), indent=2, default=str)[:1200])
            except Exception as e:
                print(f"market {mi}: {e}")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
