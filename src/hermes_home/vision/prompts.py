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
