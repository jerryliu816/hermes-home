# Home Assistant setup

The automation sends **metadata only**. It never sends image bytes — hermes-home
fetches the event still itself, which keeps the automation fast and avoids
pushing images through the automation engine.

## 1. Create a long-lived access token

Home Assistant → your profile (bottom left) → **Security** tab → **Long-lived
access tokens** → *Create token*. Copy it into `.env` as `HOME_ASSISTANT_TOKEN`.
It is shown once.

## 2. Find your real entity IDs

Developer Tools → **States**, and filter on your camera's name. For a Eufy camera
you are looking for two or three entities:

| Kind | Looks like | Used for |
|---|---|---|
| Trigger | `binary_sensor.front_door_person_detected` | fires the automation |
| Event image | `image.front_door_event_image` | the still we analyze |
| Camera | `camera.front_door` | fallback snapshot |

Check the event-image entity's **state** — it should be a timestamp such as
`2026-09-08T07:50:32.232915+00:00`, and it should change each time a new event
occurs. That timestamp is how hermes-home tells a fresh still from the previous
event's frame. If your camera does not behave this way, set
`event_image_strategy: camera_snapshot` for it in `config/cameras.yaml`.

## 3. Put the entity IDs in `config/cameras.yaml`

```yaml
cameras:
  front_door:
    name: Front Door
    camera_entity: camera.front_door                    # <- yours
    event_image_entity: image.front_door_event_image    # <- yours
    event_image_strategy: image_entity_state
    location: front_entry
    observes:
      - front_porch
      - front_walkway
```

`location` and every entry under `observes` must be a zone declared in
`config/home.yaml`, or the service refuses to start and tells you which name is
wrong.

## 4. Add the automation

Settings → Automations & scenes → Create automation → ⋮ → **Edit in YAML**.

```yaml
alias: Front Door event -> hermes-home
description: Notify hermes-home when the front door camera detects a person.
mode: queued          # a burst of detections should queue, not be dropped
max: 10

triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_person_detected   # <- yours
    to: "on"

actions:
  - action: rest_command.hermes_home_event
    data:
      event_type: camera.person_detected
      camera: front_door
      entity_id: image.front_door_event_image             # <- yours
      timestamp: "{{ now().isoformat() }}"
```

And the matching `rest_command` in `configuration.yaml`:

```yaml
rest_command:
  hermes_home_event:
    url: "http://HERMES_HOST_IP:8099/api/v1/events/home-assistant"   # <- your hermes machine
    method: POST
    content_type: "application/json"
    headers:
      X-Hermes-Webhook-Secret: !secret hermes_home_webhook_secret
    timeout: 5          # hermes-home answers in milliseconds; fail fast
    payload: >-
      {
        "event_type": "{{ event_type }}",
        "camera": "{{ camera }}",
        "entity_id": "{{ entity_id }}",
        "timestamp": "{{ timestamp }}"
      }
```

Put the secret in HA's `secrets.yaml`:

```yaml
hermes_home_webhook_secret: <the same value as WEBHOOK_SECRET in .env>
```

### Why `timeout: 5` and why this cannot hurt your house

hermes-home commits the delivery and returns `202 Accepted` in a few
milliseconds, before any image retrieval or vision analysis happens. If
hermes-home is stopped, the vision provider is down, or the machine is off, the
`rest_command` fails quickly and the automation moves on. **Home Assistant never
waits on this service, and nothing about your home's operation depends on it.**

## 5. `timestamp` must carry a UTC offset

`now().isoformat()` in Home Assistant includes one. A timestamp without an offset
is rejected with a 422 rather than guessed at — assuming a timezone here would
silently skew every later time-range query.

## Network note

Put both machines on the same subnet if you can. Delivery is then direct on the
local link, with no router or inter-VLAN rules in the path, and reachability is
straightforward in both directions. Across subnets it still works provided the
router forwards between them, but that is one more thing that can block the
webhook.

Two addresses matter, and **neither belongs in this repository** — both live in
your local, gitignored `.env`:

| What | Where it is configured | How to find it |
|---|---|---|
| The hermes machine's LAN address | `LAN_BIND_IP` in `.env` | `ipconfig getifaddr en0` (macOS) |
| Home Assistant's address | `HOME_ASSISTANT_URL` in `.env` | HA → Settings → System → Network |

Throughout this document `HERMES_HOST_IP` stands for the first of those.

One requirement: **`HOST` must be `0.0.0.0` in `.env`**, not `127.0.0.1`.
Loopback is the shipped default because this service watches a private home, and
binding it to the LAN should be a deliberate act. Nothing outside the hermes
machine can reach the webhook until you change it.

If the machine's DHCP lease moves it to a different address, update the
`rest_command` URL and `LAN_BIND_IP`. A DHCP reservation for the hermes
machine avoids that entirely.

## After a Home Assistant restart or reload

An `image.*` entity resets to state `unknown` when its integration reloads, and
stays there until the camera next fires — **but `/api/image_proxy/` keeps
serving the previous event's frame.** Verified on this system: a Quick Reload
set `image.front_door_event_image` to `unknown` while the proxy still returned
the same 10,763-byte JPEG from four hours earlier.

So the timestamp gate goes blind exactly when a stale frame is most likely.
hermes-home handles this with a second check: when the entity publishes no
usable timestamp, the fetched frame is compared byte-for-byte against the last
event stored for that entity. Identical bytes are rejected as
`rejected_stale_image` rather than filed under the current time; genuinely new
bytes are accepted normally.

Nothing to configure — but if you see `rejected_stale_image` in the deliveries
table shortly after restarting Home Assistant, this is why, and it is correct.

## Supported `event_type` values

`camera.motion`, `camera.person_detected`, `camera.vehicle_detected`,
`camera.animal_detected`, `camera.package_detected`, `camera.doorbell_pressed`.

An unknown type is rejected with a 422 listing the valid ones.

## Verifying it works

```bash
curl -i -X POST "http://$HERMES_HOST_IP:8099/api/v1/events/home-assistant" \
  -H 'Content-Type: application/json' \
  -H "X-Hermes-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"event_type":"camera.person_detected","camera":"front_door",
       "entity_id":"image.front_door_event_image",
       "timestamp":"2026-09-08T12:00:00Z"}'
```

Expect `202` and a `delivery_uid`. Then:

```bash
sqlite3 data/hermes-home.db \
  "SELECT uid, status, disposition FROM event_deliveries ORDER BY id DESC LIMIT 1;"
```
