"""Edit your League lighting here, then restart with light league stop/start."""

# Normal lighting: alive, waiting for a match, and after leaving a match.
STANDARD_BRIGHTNESS = 20
STANDARD_TEMPERATURE = 2700

# Death lighting: bright red until you respawn.
DEAD_BRIGHTNESS = 100
DEAD_COLOR = "red"

# Poll League locally twice per second. Cloud commands are sent only when a
# scene changes or a failed light is due for another attempt.
POLL_INTERVAL = 0.5
DISCONNECT_GRACE = 3.0
LIGHT_RETRY_INTERVAL = 10.0
CLOUD_REQUEST_TIMEOUT = 8.0
