from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    DATABASE_URL: str = "sqlite+aiosqlite:///orders.db"
    LOG_LEVEL: str = "INFO"
    STOCK_API_URL_IPSH: str = "http://185.63.191.2/ipsh/hs/analytics/stocks"
    STOCK_API_URL_IPD: str = "http://185.63.191.2/ipd/hs/analytics/stocks"
    STOCK_API_TOKEN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def admin_ids_list(self) -> List[int]:
        if not self.ADMIN_IDS.strip():
            return []
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip().isdigit()]


settings = Settings()
