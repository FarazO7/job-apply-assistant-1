from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "job_apply"

    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    gmail_address: str = ""
    gmail_app_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465

    alert_senders: str = ""
    resume_path: str = ""
    crawl_interval_hours: int = 2


settings = Settings()


def csv(value: str) -> list[str]:
    """Split a comma-separated env value into a clean list."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]
