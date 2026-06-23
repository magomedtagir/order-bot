import asyncio
import re
import logging
from typing import Optional

from rapidfuzz import fuzz, process as fuzz_process
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from bot.models.models import Synonym, ClientNameCache, UnknownItem

logger = logging.getLogger(__name__)

_UNITS = {
    "кг", "г", "л", "мл", "шт",
    "ведер", "вед", "мешков", "мешок",
    "кон", "канистр", "литр", "литров",
}

_NUM_RE = re.compile(r"\d+[.,]?\d*")
_NON_WORD_RE = re.compile(r"[^\w]")

FUZZY_THRESHOLD = 70


def extract_base_name(raw: str) -> str:
    s = raw.lower()
    s = _NUM_RE.sub("", s)
    words = [_NON_WORD_RE.sub("", w) for w in s.split()]
    words = [w for w in words if w and w not in _UNITS]
    return " ".join(sorted(words))


class SmartNormalizer:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

        # {client_name: {base_name: {full_name, resolved_by, confidence}}}
        self._client_cache: dict[str, dict[str, dict]] = {}

        # Справочник синонимов (для /add_synonym, не используется в поиске)
        self._synonyms: dict[str, str] = {}

        # Каталог товаров из таблиц products + product_aliases
        # alias_base → full_name
        self._alias_to_product: dict[str, str] = {}
        # Все full_name товаров (для contains-поиска и fuzzy)
        self._product_full_names: list[str] = []
        # extract_base_name(full_name) для каждого товара (для fuzzy)
        self._product_name_bases: list[str] = []

    # ── Загрузка при старте ────────────────────────────────────────────────

    async def reload(self, session: AsyncSession) -> None:
        async with self._lock:
            await self._load_client_cache(session)
            await self._load_synonyms(session)

    async def _load_synonyms(self, session: AsyncSession) -> None:
        result = await session.execute(select(Synonym))
        rows = result.scalars().all()
        count = 0
        for r in rows:
            base = extract_base_name(r.raw_name)
            if base:
                self._alias_to_product[base] = r.normalized_name
                count += 1
            if r.normalized_name not in self._product_full_names:
                self._product_full_names.append(r.normalized_name)
                self._product_name_bases.append(extract_base_name(r.normalized_name))
        logger.info("[NORMALIZER] Synonyms loaded: %d", count)

    async def _load_client_cache(self, session: AsyncSession) -> None:
        result = await session.execute(select(ClientNameCache))
        rows = result.scalars().all()
        self._client_cache = {}
        for r in rows:
            self._client_cache.setdefault(r.client_id, {})[r.base_name] = {
                "full_name": r.full_name,
                "resolved_by": r.resolved_by,
                "confidence": r.confidence,
            }
        logger.info("[NORMALIZER] Client cache loaded: %d clients", len(self._client_cache))

    # ── Запись в кэш клиента ───────────────────────────────────────────────

    async def _write_cache(
        self,
        session: AsyncSession,
        client_name: str,
        base_name: str,
        full_name: str,
        resolved_by: str,
        confidence: float,
    ) -> None:
        self._client_cache.setdefault(client_name, {})[base_name] = {
            "full_name": full_name,
            "resolved_by": resolved_by,
            "confidence": confidence,
        }
        existing = await session.execute(
            select(ClientNameCache).where(
                ClientNameCache.client_id == client_name,
                ClientNameCache.base_name == base_name,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.full_name = full_name
            row.resolved_by = resolved_by
            row.confidence = confidence
        else:
            session.add(ClientNameCache(
                client_id=client_name,
                base_name=base_name,
                full_name=full_name,
                resolved_by=resolved_by,
                confidence=confidence,
            ))

    # ── Основной метод нормализации ────────────────────────────────────────

    async def normalize(
        self,
        raw_name: str,
        client_name: str,
        session: AsyncSession,
        order_id: Optional[int] = None,
    ) -> tuple[Optional[str], bool]:
        """
        Returns (normalized_name, is_unknown).
        Adds ClientNameCache / UnknownItem entries to session (caller commits).
        """
        if not raw_name:
            return raw_name, False

        # Шаг 1 — base_name
        base = extract_base_name(raw_name)
        if not base:
            # Только цифры/юниты — вернуть как есть
            return raw_name, False

        # Шаг 2 — кэш клиента
        cached = self._client_cache.get(client_name, {}).get(base)
        if cached:
            logger.info(
                '[НОРМАЛИЗАТОР] client=%s raw="%s" base="%s" → "%s" via=cache confidence=%.2f',
                client_name, raw_name, base, cached["full_name"], cached["confidence"],
            )
            return cached["full_name"], False

        # Шаг 3 — точное совпадение base_name с псевдонимом товара
        if base in self._alias_to_product:
            full_name = self._alias_to_product[base]
            async with self._lock:
                await self._write_cache(session, client_name, base, full_name, "alias", 1.0)
            logger.info(
                '[НОРМАЛИЗАТОР] client=%s raw="%s" base="%s" → "%s" via=alias confidence=1.0',
                client_name, raw_name, base, full_name,
            )
            return full_name, False

        # Шаг 4 — нечёткое словарное сопоставление:
        # каждое слово из base_name должно нечётко совпадать хотя бы с одним словом full_name
        _WORD_THRESHOLD = 75  # минимальная схожесть одного слова
        words = base.split()
        candidates: list[tuple[str, float]] = []

        for i, fn in enumerate(self._product_full_names):
            fn_words = self._product_name_bases[i].split()
            fn_lower = fn.lower()
            all_match = True
            total_score = 0

            for qw in words:
                if qw in fn_lower:
                    # Быстрый путь: точное вхождение
                    total_score += 100
                    continue
                best = max((fuzz.ratio(qw, fw) for fw in fn_words), default=0)
                if best < _WORD_THRESHOLD:
                    all_match = False
                    break
                total_score += best

            if all_match:
                candidates.append((fn, total_score / len(words)))

        if candidates:
            # Лучший средний score, при равенстве — более короткий full_name
            full_name = max(candidates, key=lambda x: (x[1], -len(x[0])))[0]
            confidence = round(max(candidates, key=lambda x: x[1])[1] / 100, 2)
            async with self._lock:
                await self._write_cache(session, client_name, base, full_name, "contains", confidence)
            logger.info(
                '[НОРМАЛИЗАТОР] client=%s raw="%s" base="%s" → "%s" via=contains candidates=%d confidence=%.2f',
                client_name, raw_name, base, full_name, len(candidates), confidence,
            )
            return full_name, False

        # Шаг 5 — нечёткий поиск по extract_base_name(full_name) всех товаров
        # Для коротких слов снижаем порог
        threshold = 60 if len(base) < 6 else FUZZY_THRESHOLD
        if self._product_name_bases:
            result = fuzz_process.extractOne(
                base,
                self._product_name_bases,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=threshold,
            )
            if result:
                _, score, idx = result
                full_name = self._product_full_names[idx]
                confidence = score / 100
                async with self._lock:
                    await self._write_cache(session, client_name, base, full_name, "fuzzy", confidence)
                logger.info(
                    '[НОРМАЛИЗАТОР] client=%s raw="%s" base="%s" → "%s" via=fuzzy confidence=%.2f',
                    client_name, raw_name, base, full_name, confidence,
                )
                return full_name, False

        # Шаг 6 — неизвестная позиция
        logger.info(
            '[НОРМАЛИЗАТОР] client=%s raw="%s" base="%s" → UNKNOWN',
            client_name, raw_name, base,
        )
        if order_id is not None:
            session.add(UnknownItem(
                client_id=client_name,
                order_id=order_id,
                raw_name=raw_name,
                base_name=base,
            ))
        return None, True

    # ── Вспомогательные методы ─────────────────────────────────────────────

    def find_product(self, text: str) -> Optional[str]:
        """Find best matching product name without any side effects."""
        base = extract_base_name(text)
        if not base:
            return None
        if base in self._alias_to_product:
            return self._alias_to_product[base]
        words = base.split()
        candidates: list[tuple[str, float]] = []
        for i, fn in enumerate(self._product_full_names):
            fn_words = self._product_name_bases[i].split()
            fn_lower = fn.lower()
            all_match = True
            total_score = 0
            for qw in words:
                if qw in fn_lower:
                    total_score += 100
                    continue
                best = max((fuzz.ratio(qw, fw) for fw in fn_words), default=0)
                if best < 75:
                    all_match = False
                    break
                total_score += best
            if all_match:
                candidates.append((fn, total_score / len(words)))
        if candidates:
            return max(candidates, key=lambda x: (x[1], -len(x[0])))[0]
        if self._product_name_bases:
            result = fuzz_process.extractOne(
                base, self._product_name_bases,
                scorer=fuzz.token_sort_ratio, score_cutoff=70,
            )
            if result:
                return self._product_full_names[result[2]]
        return None

    async def update_from_stock(self, item_names: list[str]) -> None:
        """Merge 1C stock API item names into the in-memory product catalog."""
        async with self._lock:
            existing_bases = set(self._product_name_bases)
            added = 0
            for name in item_names:
                base = extract_base_name(name)
                if base and base not in existing_bases:
                    self._product_full_names.append(name)
                    self._product_name_bases.append(base)
                    existing_bases.add(base)
                    added += 1
        if added:
            logger.info(
                "[NORMALIZER] +%d names from stock API (total catalog: %d)",
                added, len(self._product_full_names),
            )

    def add_to_cache(self, raw_name: str, normalized_name: str) -> None:
        base = extract_base_name(raw_name)
        if not base:
            return
        self._alias_to_product[base] = normalized_name
        if normalized_name not in self._product_full_names:
            self._product_full_names.append(normalized_name)
            self._product_name_bases.append(extract_base_name(normalized_name))

    def get_client_cache(self, client_name: str) -> dict:
        return dict(self._client_cache.get(client_name, {}))

    async def clear_client_cache(self, session: AsyncSession, client_name: str) -> int:
        async with self._lock:
            count = len(self._client_cache.pop(client_name, {}))
            from sqlalchemy import delete
            await session.execute(
                delete(ClientNameCache).where(ClientNameCache.client_id == client_name)
            )
            await session.commit()
        return count

    async def prepare_resolution(
        self,
        session: AsyncSession,
        client_name: str,
        raw_name: str,
        full_name: str,
    ) -> None:
        """Write resolution to DB and update in-memory cache — caller commits."""
        base = extract_base_name(raw_name)
        async with self._lock:
            await self._write_cache(session, client_name, base, full_name, "manual", 1.0)
            if base:
                self._alias_to_product[base] = full_name
        logger.info(
            '[НОРМАЛИЗАТОР] Manual resolution: client=%s raw="%s" → "%s"',
            client_name, raw_name, full_name,
        )

    async def add_resolution(
        self,
        session: AsyncSession,
        client_name: str,
        raw_name: str,
        full_name: str,
    ) -> None:
        await self.prepare_resolution(session, client_name, raw_name, full_name)
        await session.commit()


normalizer = SmartNormalizer()
