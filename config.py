import logging
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    DATABASE_URL: str = "postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME"
    LOG_LEVEL: str = "INFO"
    STOCK_API_BASE_URL: str = "http://157.22.192.252"
    STOCK_API_BASES: str = "ipsh,ipmmg,dk,roz,ipd"
    STOCK_API_TOKEN: str = ""

    @property
    def stock_bases_list(self) -> list[str]:
        return [b.strip() for b in self.STOCK_API_BASES.split(",") if b.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def admin_ids_list(self) -> List[int]:
        if not self.ADMIN_IDS.strip():
            return []
        ids = []
        for x in self.ADMIN_IDS.split(","):
            x = x.strip()
            if x.isdigit():
                ids.append(int(x))
            elif x:
                logger.warning(
                    "ADMIN_IDS contains non-numeric token %r — ignored. "
                    "Use comma-separated Telegram user IDs (integers).",
                    x,
                )
        return ids


settings = Settings()
