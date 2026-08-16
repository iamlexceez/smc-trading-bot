# Slack Socket Mode Reference

Official sources consulted during the unified Telegram + Slack integration:

1. Slack Developer Docs — Using Socket Mode: https://docs.slack.dev/apis/events-api/using-socket-mode
2. Slack Developer Docs — Python SDK Socket Mode: https://docs.slack.dev/tools/python-slack-sdk/socket-mode
3. Slack Developer Docs — Incoming Webhooks: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks

Key verified facts:

- Socket Mode lets an app receive Events API and interactive payloads over an outbound WebSocket without exposing a public HTTP Request URL.
- Slack requires Socket Mode to be enabled in the app settings and an app-level token with the `connections:write` scope; the app-level token starts with `xapp-`.
- The Slack bot token used for Web API calls starts with `xoxb-`.
- Slack’s Python SDK provides an asyncio-compatible Socket Mode client using `AsyncWebClient` and the `slack_sdk.socket_mode.aiohttp.SocketModeClient` implementation.
- Each Socket Mode envelope must be acknowledged using its `envelope_id`; otherwise Slack can retry or treat the event as unhandled.
- Slack Socket Mode apps should expect disconnects and connection refreshes; the control adapter must isolate reconnect failures from trading.
- Incoming webhooks post JSON payloads to a channel-specific URL. Slack documents that the webhook URL is a secret and can be revoked if leaked.
- The bot’s implementation therefore uses Socket Mode for inbound commands, incoming webhooks for outbound notifications, explicit Slack user/channel allow-lists, and a central command bus that remains independent of the trading engine.
