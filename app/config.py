import os
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str = Field(
        default="postgresql://postgres:postgrespassword@db:5432/transaction_db"
    )
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    GEMINI_API_KEY: Optional[str] = Field(default=None)
    GEMINI_MODEL: str = Field(default="gemini-2.5-flash")
    UPLOAD_DIR: str = Field(default="uploads")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
