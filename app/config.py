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

    # --- finder / agent ---
    search_provider: str = "searlo"
    searlo_api_key: str = ""
    searlo_base_url: str = "https://api.searlo.tech/api/v1"
    search_site: str = "linkedin.com/posts"     # path/domain the finder targets
    search_date_range: str = "month"            # day | week | month | year (recency)
    search_results_per_query: int = 10
    agent_send_window_start: int = 10           # IST hour, inclusive (10 AM)
    agent_send_window_end: int = 17             # IST hour, exclusive (5 PM)
    agent_morning_hour: int = 10                # off-hours finds scheduled to this IST hour
    agent_scan_hours: str = "10,13,16"          # IST hours the auto-scan runs
    agent_max_actions_per_day: int = 10         # cap on auto sends + schedules per day
    searlo_daily_credit_cap: int = 30           # hard backstop on search credits per day
    job_search_queries: str = ""                # seed queries (CSV)


settings = Settings()


def csv(value: str) -> list[str]:
    """Split a comma-separated env value into a clean list."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]