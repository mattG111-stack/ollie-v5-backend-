from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "changeme"
    cors_origins: str = "http://localhost:3000"
    batch_retention_limit: int = 12  # keep last N batches per type+region
    brave_api_key: str = ""          # optional — reliable search for external estimates
    stripe_secret_key: str = ""      # optional — Stripe billing metrics on the admin dashboard

    # --- self-serve onboarding + trial billing ---------------------------------
    app_base_url: str = "http://localhost:3000"   # where Stripe redirects back after checkout
    trial_days: int = 7                            # free-trial length; first charge after this
    # Stripe subscription price the trial converts to. Create a recurring Price in
    # Stripe and put its id here; without it, checkout can't start.
    stripe_price_id: str = ""
    stripe_webhook_secret: str = ""                # verifies Stripe webhook signatures
    # Email delivery (Resend). Without a key we log the code to the server instead
    # of sending — the flow still works end-to-end in dev.
    resend_api_key: str = ""
    email_from: str = "Ollie <onboarding@resend.dev>"
    # SMS delivery (Twilio) — not wired yet; phone codes are logged for now.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    verify_code_ttl_minutes: int = 15              # how long an email/phone code stays valid

    @property
    def cors_origin_list(self) -> list[str]:
        return [s.strip() for s in self.cors_origins.split(",") if s.strip()]


settings = Settings()
