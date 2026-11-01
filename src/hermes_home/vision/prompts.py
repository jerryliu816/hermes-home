"""Prompt text for scene analysis.

``PROMPT_VERSION`` is recorded on every analysis row. It costs one string and it
is the only thing that will later explain why last month's observations read
differently from this month's.

The null licensing in the prompt is load-bearing, not politeness. If the model is
not explicitly told that null is the correct answer for an occluded or ambiguous
scene -- and that guessing is worse -- it returns confidently hallucinated zeros,
and the entire unknown-handling design becomes decorative.

A camera may add its own ``vision_context`` (see ``CameraConfig``). That text is
appended to the base prompt rather than replacing any of it, and the null,
confidence, tag-vocabulary and no-guessing rules are restated *after* it. A
camera pointed inside a reptile enclosure should change what is worth describing;
it must not be able to change what counts as knowing something.

To see exactly what a camera will be sent, against the running container's own
configuration (substitute any camera key)::

    docker compose exec -T hermes-home python -c "
    from pathlib import Path
    from hermes_home.config import load_cameras_config
    from hermes_home.core.time import now_utc
    from hermes_home.vision.base import CameraEventContext
    from hermes_home.vision.prompts import build_system_prompt, build_user_prompt
    c = load_cameras_config(Path('/app/config')).cameras['gecko']
    ctx = CameraEventContext(camera_key='gecko', camera_name=c.name,
                             zone_name=c.location, observes=c.observes,
                             event_type='camera.motion', occurred_at=now_utc(),
                             vision_context=c.vision_context)
    print('=== SYSTEM ==='); print(build_system_prompt(ctx))
    print('=== USER ==='); print(build_user_prompt(ctx))
    "

Both matter: the system prompt carries the rules and any camera context, the user
prompt grounds the model in this one frame.
"""

from __future__ import annotations

from hermes_home.vision.base import CameraEventContext

PROMPT_VERSION = "scene-v1"

#: Recorded instead of ``PROMPT_VERSION`` on analyses made through Ollama. The
#: system and user prompts are the same builders' output, byte for byte, but
#: Ollama's user message also carries :data:`OLLAMA_JSON_GUIDANCE`, so the text
#: the model actually saw differs and the provenance has to say so. Bump the
#: suffix when that guidance changes; bump the base when the shared prompt does.
#:
#: History: under ``ollama-json-1`` gemma4:31b returned lighting, importance,
#: overall_confidence and activity as null on all six real frames tried, even in
#: daylight. The cause was the format schema, not the wording: those keys were
#: optional and the model omitted them. ``ollama-json-2`` sends every key as
#: required (still nullable; see vision/ollama.py) and words the guidance to
#: separate frame-level fields from counts and yes/no fields.
#: ``ollama-json-3`` adds what each lighting value means (a porch-lit night frame
#: had come back "daylight" with high confidence), when confidence may be high,
#: and a suggested set of activity labels (the free-text field had drifted:
#: "person walking", "person_standing", "person_present").
#: ``ollama-json-4`` restores null counts for frames darkness hides: under json-3
#: a nearly black enclosure frame came back with 0 people and 0 vehicles -- a
#: guessed zero, the exact failure the null rules exist to prevent.
OLLAMA_PROMPT_VERSION = f"{PROMPT_VERSION}+ollama-json-4"

BASE_VISION_PROMPT = """\
You analyze a single still frame from a residential security camera and return \
structured observations.

REPORTING UNKNOWNS — this matters more than completeness:
- Use null for any field you cannot determine from this image. Null is a correct,
  expected answer, not a failure.
- Guessing is worse than null. Never infer a count from context, from what is
  usually true, or from part of a scene you cannot actually see.
- Null and zero mean different things. Use 0 only when you can see that none are
  present. Use null when you cannot tell.
- When a field is null for an interesting reason (occlusion, glare, darkness,
  something leaving the frame), record the reason in field_notes.

PRIVACY — these are firm limits:
- Never attempt to identify a specific person. Do not name anyone, do not guess
  whether someone is a resident, a known visitor, or has been seen before.
- Do not describe biometric characteristics for identification purposes.
- Do not read or record license plate numbers.
- Describing appearance is fine and useful: "a person in a blue jacket carrying a
  cardboard box" is exactly right.

STYLE:
- scene_summary is one or two plain sentences of what is visible. No speculation
  about intent or motive.
- notable_attributes holds short descriptive phrases.
- tags are flat labels from: person_present, vehicle_present, animal_present,
  package_present, door_open, delivery, low_visibility. Emit a tag only when you
  are confident it is true; omit it otherwise. Never emit a tag to mean "absent".
"""

#: Header introducing a camera's own ``vision_context``.
CAMERA_CONTEXT_HEADER = "\n\nCAMERA-SPECIFIC CONTEXT:\n"

#: Restated after the camera context, so the appended text cannot be read as
#: permission to relax the parts that make an observation trustworthy. Ordering
#: is the whole mechanism here: last word wins in a prompt, and these rules get it.
CAMERA_CONTEXT_GUARDRAIL = """

The camera-specific context above changes what is worth describing -- the emphasis
of scene_summary, activity, notable_attributes and tags. It does not change any of
the rules above it, and it never overrides them:
- Null still means unknown. Continue to use null for anything this frame does not
  show, and never let the context supply a value the image does not.
- Confidence still reflects what is visible in the image, not what the context
  says is expected there.
- The tag vocabulary is unchanged; the context may not introduce new tags.
- The privacy limits are unchanged.
- Return the same structured schema regardless of any camera-specific context.
"""


def build_system_prompt(context: CameraEventContext) -> str:
    """The base prompt, plus this camera's own context when it has one.

    Cameras without ``vision_context`` get a byte-identical prompt to the one they
    got before this existed -- which is what keeps ``PROMPT_VERSION`` honest for
    them.
    """
    if not context.vision_context or not context.vision_context.strip():
        return BASE_VISION_PROMPT
    return (
        BASE_VISION_PROMPT
        + CAMERA_CONTEXT_HEADER
        + context.vision_context.strip()
        + CAMERA_CONTEXT_GUARDRAIL
    )


def build_user_prompt(context: CameraEventContext) -> str:
    """Ground the model in where the camera is and what triggered it."""
    lines = [
        f"Camera: {context.camera_name}",
        f"Trigger: {context.event_type}",
        f"Time (UTC): {context.occurred_at.isoformat()}",
    ]
    if context.zone_name:
        lines.append(f"Camera location: {context.zone_name}")
    if context.observes:
        lines.append(f"Areas in view: {', '.join(context.observes)}")
    lines.append(
        "\nDescribe what is visible in this frame. Use null for anything you cannot "
        "determine with confidence."
    )
    return "\n".join(lines)


#: Appended to the user prompt for Ollama only. Anthropic's structured-output
#: API binds the schema itself. Ollama constrains decoding with ``format``, and
#: Ollama's own structured-output guidance also recommends saying so in the
#: prompt. It restates, and never relaxes, the null rules.
OLLAMA_JSON_GUIDANCE = """

Reply with exactly one JSON object that matches the provided schema and nothing \
else: no prose, no markdown.

Two kinds of field, treated differently:
- Frame-level fields describe the whole image and can be read from any frame \
you can see at all: lighting, importance, overall_confidence and activity. \
Always fill them. Leave these null only if the image is unreadable.
- Counts and yes/no fields (person_count, vehicle_count, animal_count, \
package_present) are null when this frame does not let you determine them, and \
0 or false only when you can see that none are present. When darkness, glare \
or blur hides most of the scene, they are null even if nothing is visible: \
seeing nothing in a frame you cannot see into is not seeing that none are \
there. Give the reason in field_notes. A dark frame can still have readable \
areas; use 0 or false only for what those areas clearly show.

lighting describes the light in this image, judged only from the image:
- "daylight": lit by the sun or a bright sky.
- "artificial": a night or indoor scene lit by lamps, porch lights or other \
fixtures; the surroundings beyond the lit area are dark.
- "low_light": dim but readable, such as dusk, dawn or deep shade.
- "dark": little is visible. A grayscale night-vision (infrared) frame is \
"dark" or "low_light", never "daylight".

overall_confidence is "high" only when the frame is sharp, well lit and \
unobstructed. Use "medium" for blur, distance or partial occlusion, and "low" \
when darkness or blur hides most of the scene.

activity is a short lowercase snake_case label for what is happening, not for \
who is present. Prefer one of: walking, standing, passing_by, visitor_at_door, \
package_delivery, package_pickup, vehicle_arriving, vehicle_leaving, \
animal_activity, no_activity. If none fits, use a short label of your own in \
the same style.

field_notes explains why a field is null. Add an entry only for a field you \
left null, keyed by that field's name, with a short reason."""
