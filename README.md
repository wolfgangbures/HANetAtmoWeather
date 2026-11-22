# Netatmo Indoor Climate

**v0.1.0-beta.1** - an experimental beta. Signed webhook reception and
registration are tested with synthetic responses; real NAMain event payloads
and delivery cadence still need verification.

A small Home Assistant custom integration focused **only** on the indoor unit:

- Humidity (%)
- Noise (dB)
- Last measurement (diagnostic timestamp from Netatmo, not the polling time)

Choose either a **Healthy Home Coach / Smart Indoor Air Quality Monitor (NHC)**
or a **weather station indoor base unit (NAMain)**. One unit is selected per
integration entry. Add another entry for another unit. Outdoor modules, additional
weather modules, thermostats, cameras, and public weather are deliberately excluded.
NAMain is the default selection and has a signed webhook receiver from initial
setup. NHC remains polling-only because it is not listed as supported by the new
Netatmo webhook service.

## Faster updates: what is and is not possible

The integration polls once every **60 seconds** by default, adjustable to **30-600
seconds** in its options. All three entities share one API request per poll.
That is 60 requests/hour per unit at the default, or 120 at the minimum, excluding
setup, webhook registration, and OAuth token refresh. Quotas are shared with
other clients using your Netatmo app/account. HTTP 429 and Netatmo quota errors
pause requests, including manual refreshes, until the cooldown expires.
The cooldown is shared by this integration's entries and survives entry setup
retries and reloads (but not a Home Assistant restart).

This reduces the delay *after a new measurement becomes available in the cloud*.
It does **not** force a new reading, enable local access, or provide live audio.
Netatmo documents weather station dashboard cloud updates as approximately every
10 minutes. Its near-real-time `homestatus` endpoint is not documented as a source
for these humidity/noise measurements, so this integration does not promise
near-real-time readings or use undocumented endpoints.

For comparison, Home Assistant Core 2026.1 schedules weather dashboard retrieval
every 600 seconds and Air Care every 300 seconds. These are version-specific
defaults, not a guarantee about your installation.

Check **Last measurement** and the `measurement_time` attribute to determine
whether more frequent polling actually helps your unit. Measurements older than
20 minutes, future-dated measurements, or unreachable units make humidity/noise
unavailable. Missing values remain unknown, never zero. The diagnostic timestamp
remains visible on a successful poll even if the measurement is stale.

## Installation and authorization

Requires Home Assistant **2026.1 or newer**.

1. Download `netatmo_indoor.zip` from the
   [GitHub releases](https://github.com/wolfgangbures/HANetAtmoWeather/releases)
   and extract it into `/config/custom_components/`, resulting in
   `/config/custom_components/netatmo_indoor/manifest.json`.
   Alternatively, copy `custom_components/netatmo_indoor` from this repository
   into your Home Assistant `/config/custom_components/` directory.
2. Restart Home Assistant.
3. Create a developer application at [Netatmo Connect](https://dev.netatmo.com/).
   Set its redirect URI to `https://my.home-assistant.io/redirect/oauth` when
   using Home Assistant's **My** integration. Without My, use your Home Assistant
   base URL plus `/auth/external/callback`.
4. In **Settings > Devices & services**, open the menu > **Application
   credentials** and add credentials for **Netatmo Indoor Climate**.
   Keep the client secret private; do not put it in this repository.
5. **Add integration > Netatmo Indoor Climate**, choose the product family,
   authorize with Netatmo, then select your indoor unit.
6. Use the integration's options to change the polling interval.

## NAMain webhooks (currently diagnostic logging)

Configure a **publicly reachable external HTTPS Home Assistant URL** in
**Settings > System > Network** before adding the integration. Ensure your
reverse proxy forwards `/api/webhook/` and trusts/configures proxies correctly.
A configured URL alone does not establish public reachability. Nabu Casa's
remote-access URL is not automatically provisioned as a cloudhook by this version.

On each load the receiver is installed and the integration checks
`GET https://api.netatmo.com/webhooks/v1/`. If the exact callback URL is not
registered, it appends it with `POST /webhooks/v1/` and `{"url": "..."}`.
Existing subscriptions are never replaced or deleted. Your developer app must
be allowed to use the new Webhooks API; Netatmo may reject registration otherwise.
Failures are explicitly logged and do not stop sensor polling. Once corrected,
reload the integration to retry (wait for quota cooldown if applicable).

The callback URL is shown in the NAMain integration's options after setup.
Its random ID is persisted across reloads/restarts; keep the URL private.
For manual configuration you can use that URL in Netatmo's developer app
settings; Netatmo says users must reauthorize after changing the app-level URL.
Prefer a dedicated developer app, particularly if the built-in integration also
uses webhooks. The new API's user-level subscriptions can coexist, subject to
Netatmo's subscription limit.

Incoming POST bodies are bounded to 64 KiB and verified using Netatmo's documented
`X-Netatmo-secret` HMAC-SHA256 header and the developer application's client
secret. Unsigned/tampered requests are rejected. No second copy of the client
secret is stored in the config entry. Unloading removes the local receiver;
remote subscriptions are retained to avoid deleting other clients' callbacks.
If removing the integration permanently, review/remove its subscription in
Netatmo's app/API tools. Changing the external URL may leave an old subscription.

At this stage **events do not change sensors or trigger extra polls**. INFO logs
confirm activation/deactivation and events for the selected unit. DEBUG logs show
only recognized numeric climate/timestamp fields from the selected indoor module,
including nested dashboard/measurement objects. Outdoor telemetry, account IDs,
device IDs, home names, credentials, signatures, callback URLs, and arbitrary
payload values are not logged by this integration. Home Assistant's own webhook
logging may include callback IDs, so treat exported logs as private.

Enable diagnostic logging with:

```yaml
logger:
  logs:
    custom_components.netatmo_indoor: debug
```

Watch for `Netatmo indoor webhook subscription confirmed` and
`Indoor webhook telemetry (selected unit only)`. Empty telemetry means the event
had no recognized selected-module fields; it does not prove no measurements exist.
We need a real NAMain event to confirm humidity/noise field shapes and cadence
before mapping webhook values to entities. Webhooks do not guarantee a higher
hardware sampling rate. Polling remains the source of sensor readings.

The integration requests only `read_homecoach` or `read_station`, according to
your product selection. Home Assistant manages and persists OAuth access/refresh
tokens and prompts for reauthorization if access is revoked.

It can coexist with the built-in Netatmo integration under a separate domain,
but both will consume quota and create separate humidity/noise entities.
Existing automations are not migrated automatically. No changes are made to
your running Home Assistant by creating these files.

## Development

On a supported Linux Python environment:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
python -m pytest
ruff check .
ruff format --check .
```

The tests use synthetic Netatmo responses; no credentials or real device are
required. Hardware upload frequency and end-to-end OAuth authorization must be
verified on your own Home Assistant installation.
Local Windows validation used Python 3.13 with `lru-dict` 1.4.1 instead of Core's
1.3.0 pin, because the older dependency requires an unavailable C compiler.
The integration itself adds no third-party runtime dependencies.

## API references

- [Netatmo Weather API](https://dev.netatmo.com/apidocumentation/weather)
- [Netatmo Aircare API](https://dev.netatmo.com/apidocumentation/aircare)
- [Netatmo Webhooks API and signature verification](https://dev.netatmo.com/apidocumentation/webhooks)
- [Home Assistant OAuth2 guidance](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#configuration-via-oauth2)
