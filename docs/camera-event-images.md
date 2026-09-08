# Camera event images — observed Eufy behavior

Measured against the real Front Door camera (Eufy via `eufy-security-ws`), not
assumed. Recorded here because it is the evidence a future image-source policy
should be built on.

## Timing: the trigger beats the picture

| Offset from trigger | What happens |
|---|---|
| `t+0.0s` | `binary_sensor.front_door_person_detected` → `on`; HA fires the webhook |
| `t+0.0s` → `t+2.3s` | `image.front_door_event_image` reports `unknown` — the still is still uploading |
| **`t+2.3s`** | **first frame published — 288×176, ~12 KB** |
| **`t+24.8s`** | **second frame published — 640×720, ~58 KB, same event** |

The second publication was confirmed to belong to the same event: exactly one
webhook was received, and no second detection fired.

Two consequences, both already handled:

1. **The detection fires before the image exists.** A freshness gate that gives
   up in under ~3s rejects perfectly good events. The budget is 15s by default
   (`FRESHNESS_POLL_ATTEMPTS` × `FRESHNESS_POLL_INTERVAL_SECONDS`).
2. **`unknown` means "not here yet"**, not "unavailable" — so it is a reason to
   keep waiting, not to give up.

## Resolution: two stages, not one

The event image entity publishes **twice** for a single event: a low-resolution
thumbnail almost immediately, then a full-resolution frame roughly 20+ seconds
later. This is a single observation and should be re-confirmed against naturally
occurring events before anything depends on it.

V1 deliberately takes the **first** frame (288×176). It is the correct moment, it
arrives with the event, it costs no battery and opens no P2P stream, and its
freshness behaviour is empirically validated.

### If 288×176 proves too coarse for vision

Three options, in increasing cost — note that the first is both the cheapest and
the most promising, and it was not obvious before measuring:

1. **Wait for the second publication of the same entity.** Same moment, same
   zero battery cost, ~9× the pixels. Costs ~25s of latency on the background
   worker, which the caller never feels because the webhook was acknowledged in
   milliseconds. This is the option to try first.
2. **Analyze the thumbnail, then escalate.** Run vision on the fast frame; if
   confidence is low or a detail matters, fetch the higher-resolution frame and
   re-analyze. `event_analyses` already stores one row per attempt, so a second
   analysis of the same event needs no schema change.
3. **Fall back to `camera.front_door`** (1280×1440). Highest detail, but it is a
   *live* snapshot taken seconds after the fact — the wrong moment, and it wakes
   the camera. Available today by setting `event_image_strategy: camera_snapshot`.

None of this is implemented. The seam that makes it a contained change is
`CameraConfig.event_image_strategy` plus `fetch_event_image()`, which is the only
place that decides which bytes represent an event.
