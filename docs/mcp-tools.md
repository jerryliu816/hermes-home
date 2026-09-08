# MCP tools

hermes-home exposes its semantic history to the Hermes Agent over MCP
(Streamable HTTP). Every tool is deterministic — none of them calls a language
model. Hermes does the reasoning and the prose; this server hands it structured
facts.

## Scope, and what is deliberately absent

Hermes already owns Home Assistant's *current* state through its own
`ha_get_state`, `ha_list_entities` and `ha_call_service` tools. Nothing here
duplicates that. There is no entity passthrough, no service calls, no device
control, no raw SQL, and no generic database access.

| Layer | Owns |
|---|---|
| Home Assistant | device connectivity, deterministic automation |
| Hermes' HA tools | current state, direct device interaction |
| **hermes-home (these tools)** | **persistent semantic history, spatial topology, incidents, query primitives** |
| Hermes | reasoning, natural-language synthesis |

## Registration

```bash
hermes mcp add hermes-home --url http://localhost:8099/mcp/
```

which writes to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  hermes-home:
    url: http://localhost:8099/mcp/
    enabled: true
```

Verify with `hermes mcp test hermes-home`.

DNS-rebinding protection is enabled on the endpoint. `localhost` and `127.0.0.1`
are always accepted; add any other Host value a client will send via
`MCP_ALLOWED_HOSTS` in `.env`.

## Two conventions that matter to a reader

**Only `uid` is exposed.** Database row ids never leave the service, so it stays
free to renumber or re-import without breaking a caller.

**Unknown is an explicit `null`, never a missing key.** A model reading a result
will infer "zero" from an absent field. `person_count: null` means *not
determinable from the image*; `person_count: 0` means *none were visible*. These
are different claims and the distinction is preserved end to end.

## The tools

### `home_recent_events(minutes=60, camera=None, zone=None, limit=20)`

Events in the last N minutes, newest first. The "what just happened" tool.

### `home_search_events(start_time, end_time, camera, zone, event_type, tags, limit=50)`

Time-range search. Times are ISO 8601 **with a UTC offset** — an offsetless
timestamp is rejected rather than guessed at. `event_type` matches by prefix, so
`camera.` matches every camera event. `tags` is an AND.

### `home_get_event(uid)`

Full detail for one event. Returns `{"found": false, "event": null}` rather than
erroring when the uid is unknown.

### `home_list_zones()`

Every zone, how zones connect, and which cameras observe each.

### `home_describe_home()`

Property layout plus `unobserved_zones` — the zones no camera watches. This
field exists so an agent can distinguish *nothing was recorded* from *nothing
happened*.

### `home_summarize_activity(start_time, end_time, zone, limit=100)`

Deterministic tallies for a window: counts by type, zone, camera and tag, an
incident count, and the events in chronological order. Structured counts only,
never prose. `note` is set when the result was truncated, so a partial tally is
never silently reported as complete.

## Example result

`home_recent_events(minutes=1440)`:

```json
{
  "window_minutes": 1440,
  "count": 1,
  "events": [
    {
      "uid": "10954427-05c3-4428-8018-950320741258",
      "event_type": "camera.person_detected",
      "source": "home_assistant",
      "source_entity_id": "image.front_door_event_image",
      "camera": "front_door",
      "zone": "front_entry",
      "zone_name": "Front Entry",
      "occurred_at": "2026-09-08T15:47:00.407990Z",
      "occurred_at_local": "2026-09-08T08:47:00.407990-07:00",
      "received_at": "2026-09-08T15:47:00.639353Z",
      "tags": ["package_present", "person_present"],
      "summary": "1 person(s) visible at Front Door.",
      "analysis": {
        "status": "ok",
        "provider": "mock",
        "model": "mock-scene-v1",
        "prompt_version": "scene-v1",
        "attempt": 1,
        "observation": {
          "scene_summary": "1 person(s) visible at Front Door.",
          "person_count": 1,
          "vehicle_count": null,
          "animal_count": null,
          "package_present": true,
          "activity": "package_delivery",
          "lighting": "artificial",
          "importance": "normal",
          "overall_confidence": "medium",
          "notable_attributes": ["person in a dark jacket"],
          "tags": ["person_present", "package_present"],
          "field_notes": {"vehicle_count": "mock provider does not evaluate vehicles"}
        },
        "error_code": null,
        "latency_ms": 0
      },
      "duplicate_count": 0,
      "incident_uid": "869cbb60-0dd2-4292-a754-2e22c705fbbf"
    }
  ]
}
```

`provider` and `model` travel with every observation on purpose: an agent can
then say how much weight the interpretation deserves, and in practice Hermes
does — it volunteered that a mock provider produced the reading above.

## Privacy

Observations describe appearance, never identity. There is no facial
recognition, no biometric matching, no plate database, and no persistent identity
across events. The `SceneObservation` schema is `extra="forbid"` and contains no
identity field, so a model has nowhere to record one even if it tried.

Event images are analyzed and discarded; only the derived observation, a content
hash, and image dimensions are retained.
