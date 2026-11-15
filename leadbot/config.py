"""All configuration comes from environment variables (or a local .env file). Nothing is hard-coded elsewhere."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Slack
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    slack_base_url: str = "https://slack.com/api"
    slack_alert_channel: str = "#lead-research-alerts"

    # Clay: we POST to a table's webhook source; an HTTP API column in that table calls us back.
    clay_webhook_url: str = ""
    clay_webhook_token: str = ""  # optional, if the Clay webhook source requires an auth token
    clay_callback_secret: str = ""  # Clay must send this in the X-Clay-Secret header

    # HubSpot
    hubspot_token: str = ""
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_dnc_property: str = "hs_email_optout"  # the contact property that means "do not contact"
    hubspot_linkedin_property: str = "hs_linkedin_url"  # written on new contacts; empty string to skip

    # Model provider 1: any OpenAI-compatible chat endpoint (NVIDIA NIM, OpenRouter, ...).
    # Used when LLM_API_KEY and LLM_MODEL are both set; it then takes priority over Claude.
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""  # the provider's model id, copied from its catalog
    llm_max_tokens: int = 2000  # reasoning models spend part of this thinking before they answer
    llm_extra_body: str = ""  # optional JSON merged into each request, for model-specific switches

    # Model provider 2: Claude. With no key for either provider, every brief comes from the template.
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    claude_max_tokens: int = 2000  # covers the model's thinking plus a ~120-word brief
    claude_effort: str = "low"  # low | medium | high. Sonnet 5.5 rejects a custom temperature; effort is the dial.

    # Storage: a Postgres connection string (Supabase pooler). Empty = in-memory store, for local demos only.
    database_url: str = ""

    # Behaviour
    cache_ttl_days: int = 30
    recent_contact_days: int = 30
    max_concurrent_jobs: int = 5  # caps parallel HubSpot and model calls at 10x volume
    enrich_timeout_minutes: int = 10
    stuck_job_minutes: int = 5
    max_attempts: int = 5  # runs per job. A normal cache-miss job uses 2: one up to Clay, one after the callback.
    internal_secret: str = ""  # header secret for POST /internal/sweep

    # Who is selling. This goes into the fact sheet, so the brief may mention it.
    seller_name: str = "Northwall"
    seller_pitch: str = "compliance automation for ISO 27001, SOC 2, GDPR and TISAX"

    log_level: str = "INFO"
