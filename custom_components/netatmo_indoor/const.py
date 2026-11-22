"""Constants for the indoor-only integration."""

DOMAIN = "netatmo_indoor"
CONF_PRODUCT = "product"
CONF_DEVICE_ID = "device_id"
CONF_POLL_INTERVAL = "poll_interval"
CONF_WEBHOOK_ID = "webhook_id"
WEBHOOK_API_URL = "https://api.netatmo.com/webhooks/v1/"
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 600
MAX_MEASUREMENT_AGE = 20 * 60
API_URL = "https://api.netatmo.com/api"
AUTHORIZE_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"
PRODUCTS = {
    "home_coach": ("gethomecoachsdata", "read_homecoach", "NHC"),
    "weather_station": ("getstationsdata", "read_station", "NAMain"),
}
