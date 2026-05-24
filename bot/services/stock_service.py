import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx
from rapidfuzz import process as fuzz_process, fuzz

from bot.services.normalizer import extract_base_name

logger = logging.getLogger(__name__)

STOCK_MATCH_THRESHOLD = 65


class StockChecker:
    REFRESH_INTERVAL = 300  # seconds

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._item_names: list[str] = []
        self._item_bases: list[str] = []
        self._quantities: list[float] = []
        self._last_refresh: Optional[datetime] = None

    async def _fetch(self, client: httpx.AsyncClient, url: str, token: str) -> list[dict]:
        try:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("[STOCK] Failed to fetch %s: %s", url, exc)
            return []

    async def refresh(self, url_ipsh: str, url_ipd: str, token: str) -> int:
        async with httpx.AsyncClient() as client:
            data_ipsh, data_ipd = await asyncio.gather(
                self._fetch(client, url_ipsh, token),
                self._fetch(client, url_ipd, token),
            )

        merged: dict[str, float] = {}
        for item in data_ipsh + data_ipd:
            name = item.get("ItemName", "").strip()
            qty = float(item.get("Quantity") or 0)
            if name:
                merged[name] = merged.get(name, 0) + qty

        async with self._lock:
            self._item_names = list(merged.keys())
            self._item_bases = [extract_base_name(n) for n in self._item_names]
            self._quantities = [merged[n] for n in self._item_names]
            self._last_refresh = datetime.utcnow()

        count = len(self._item_names)
        logger.info("[STOCK] Refreshed: %d unique items (ipsh=%d, ipd=%d)", count, len(data_ipsh), len(data_ipd))

        # Обновляем каталог нормализатора именами из 1С
        try:
            from bot.services.normalizer import normalizer
            await normalizer.update_from_stock(self._item_names)
        except Exception as exc:
            logger.warning("[STOCK] Failed to update normalizer catalog: %s", exc)

        return count

    async def ensure_fresh(self, url_ipsh: str, url_ipd: str, token: str) -> None:
        needs_refresh = (
            self._last_refresh is None
            or (datetime.utcnow() - self._last_refresh).total_seconds() > self.REFRESH_INTERVAL
        )
        if needs_refresh:
            await self.refresh(url_ipsh, url_ipd, token)

    def check(self, normalized_name: str) -> tuple[bool, Optional[float]]:
        """
        Returns (in_stock, quantity).
        - in_stock=True, qty=X  → found, X units available
        - in_stock=False, qty=0 → found, out of stock
        - in_stock=True, qty=None → not found in stock data (don't flag)
        """
        if not self._item_names:
            return True, None

        base = extract_base_name(normalized_name)
        if not base:
            return True, None

        match = fuzz_process.extractOne(
            base,
            self._item_bases,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=STOCK_MATCH_THRESHOLD,
        )
        if not match:
            return True, None

        _, _, idx = match
        qty = self._quantities[idx]
        logger.debug("[STOCK] '%s' → '%s' qty=%.2f", normalized_name, self._item_names[idx], qty)
        return qty > 0, qty

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    @property
    def item_count(self) -> int:
        return len(self._item_names)


stock_checker = StockChecker()
