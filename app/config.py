from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Okoone CRM"
    API_KEY: str
    DATABASE_URL: str = "sqlite:///db/okoone_crm.sqlite"
    HOST: str = "0.0.0.0"
    PORT: int = 4567

    AZURE_COMMS_CONNECTION_STRING: Optional[str] = None
    AZURE_COMMS_MAIL_FROM: Optional[str] = None

    FERNET_KEY: str

    LINKEDIN_DAILY_SEARCH_LIMIT: int = 30
    LINKEDIN_DAILY_PROFILE_LIMIT: int = 80

    ANTHROPIC_API_KEY: str = ""

    LEARNING_REVIEW_THRESHOLD: int = 20
    SCRAPE_INTERVAL_MINUTES: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
