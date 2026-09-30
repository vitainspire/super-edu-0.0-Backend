"""The context profile, and the provenance every field of it carries.

§3.2 lists what Node 2 receives about the room; §4 says every contextual field
must record how trustworthy it is. This module is both, because separating them
is what lets a field arrive without its provenance — and a field without
provenance is indistinguishable from a verified one by the time it reaches a
prompt.

THE HARD RULE, stated where it is enforced rather than only where it is written:

    High model confidence cannot turn an assumed or unknown context field into
    verified local knowledge.

They are different axes. `provenance` is a property of the DATA and only its
origin can change it. `confidence` is a property of the model's PROPOSAL and only
the model sets it. A 0.97 adaptation resting on an assumed field is a confident
guess about a guess, and `gate.py` refuses it on the provenance alone without
ever looking at the number.

WHAT NORMALIZATION IS FOR. Everything reaching this module comes from an admin
portal, a teacher form, a school setup record or a measurement, in whatever shape
that system happens to use. Downstream — activation, the prompt, the gate — reads
one shape. Doing that conversion in one place means a new intake surface changes
this file and nothing else, and it means the "unknown remains unknown" rule
(§2, Evidence before adaptation) is applied once rather than per reader.

FOUR THINGS ARE DROPPED HERE, AND SILENTLY IS THE RIGHT WORD FOR NONE OF THEM —
each is counted into `dropped` so the run can say what it refused:

  * a field whose value is empty. Nothing to stand on.
  * a field whose provenance is `unknown`. §4's whole point.
  * a field naming no factor's evidence. It would cost prompt tokens to say
    nothing, which is §14's "deterministic preprocessing removes irrelevant
    context before the LLM sees it".
  * a LOCAL-tier field that is not `verified`. This is the one that matters
    most: it is the difference between contextual reinforcement and the
    stereotype engine §7 of the research framework says the model is not.
"""
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from . import factors as factors_module

# ── §4: the provenance model ─────────────────────────────────────────────────
VERIFIED, ASSUMED, UNKNOWN = "verified", "assumed", "unknown"
PROVENANCE = (VERIFIED, ASSUMED, UNKNOWN)

# Where a field came from. Not decoration: `measured` and `admin` are the two
# that can carry a LOCAL-tier field, and a reviewer asking "who said the
# community works in fishing" wants a name rather than a boolean.
ADMIN, TEACHER, SCHOOL_SETUP, MEASURED, SYSTEM = (
    "admin", "teacher", "school_setup", "measured", "system")
ORIGINS = (ADMIN, TEACHER, SCHOOL_SETUP, MEASURED, SYSTEM)

# Past this, a "verified" field is verified about a room that may have changed.
# A year, because the things this profile holds — class size, medium of
# instruction, whether there is a projector — move on a school-year cadence, and
# a shorter window would turn every September into a cold start.
STALE_AFTER_DAYS = 400


class Field(NamedTuple):
    """One context fact, with everything §4 asks of it."""
    name: str
    value: Any
    provenance: str
    origin: str
    updated_at: Optional[str]   # ISO 8601, or None when the source recorded none

    @property
    def verified(self) -> bool:
        return self.provenance == VERIFIED

    def age_days(self) -> Optional[int]:
        if not self.updated_at:
            return None
        try:
            stamp = datetime.fromisoformat(str(self.updated_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).days

    @property
    def stale(self) -> bool:
        age = self.age_days()
        return age is not None and age > STALE_AFTER_DAYS

    def as_dict(self) -> dict:
        return {"value": self.value, "provenance": self.provenance,
                "origin": self.origin, "updatedAt": self.updated_at,
                "stale": self.stale}


def _empty(value: Any) -> bool:
    """Is there anything here to reason from?

    `0` and `False` are answers — a class with zero devices and a school with no
    electricity are exactly the facts the feasibility factors exist to read, and
    treating them as missing would silently promote "we checked, there are none"
    into "nobody said", which is the difference between a fallback being required
    and a fallback being invented.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _coerce(raw: Any) -> tuple[Any, str, str, Optional[str]]:
    """Read one intake value into (value, provenance, origin, updated_at).

    A BARE VALUE IS `assumed`, NOT `verified`, and that default is the whole
    reason this function exists rather than a dict comprehension. A caller that
    forgets to state provenance is a caller whose data has not been confirmed by
    anybody; defaulting the other way would make the safest-looking intake code
    produce the least safe profile.
    """
    if isinstance(raw, dict) and "value" in raw:
        provenance = str(raw.get("provenance") or ASSUMED).lower()
        origin = str(raw.get("origin") or SYSTEM).lower()
        return (
            raw.get("value"),
            provenance if provenance in PROVENANCE else ASSUMED,
            origin if origin in ORIGINS else SYSTEM,
            raw.get("updatedAt") or raw.get("updated_at"),
        )
    return raw, ASSUMED, SYSTEM, None


class ContextProfile:
    """The normalized profile: `fields` is all that anything downstream reads."""

    def __init__(self, fields: dict[str, Field], *, dropped: list[dict],
                 school_id: str = None, class_id: str = None):
        self.fields = fields
        self.dropped = dropped
        self.school_id = school_id
        self.class_id = class_id

    # ── reading ──────────────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[Field]:
        return self.fields.get(name)

    def value(self, name: str, default=None):
        field = self.fields.get(name)
        return default if field is None else field.value

    def has(self, name: str) -> bool:
        return name in self.fields

    def verified_names(self) -> set[str]:
        return {n for n, f in self.fields.items() if f.verified}

    def present(self, names) -> list[str]:
        return [n for n in names if n in self.fields]

    def verified_present(self, names) -> list[str]:
        return [n for n in names if n in self.fields and self.fields[n].verified]

    # ── writing out ──────────────────────────────────────────────────────────
    def as_dict(self) -> dict:
        return {
            "schoolId": self.school_id,
            "classId": self.class_id,
            "fields": {n: f.as_dict() for n, f in sorted(self.fields.items())},
            "dropped": self.dropped,
        }

    def summary(self) -> dict:
        verified = sum(1 for f in self.fields.values() if f.verified)
        return {
            "fields": len(self.fields),
            "verified": verified,
            "assumed": len(self.fields) - verified,
            "stale": sum(1 for f in self.fields.values() if f.stale),
            "dropped": len(self.dropped),
        }


def normalise(raw: dict, *, school_id: str = None, class_id: str = None,
              teacher_settings: dict = None) -> ContextProfile:
    """Build a profile from whatever the intake surface supplies.

    `teacher_settings` is folded in as a SECOND-CLASS source, and the ranking is
    deliberate: those values (class size, duration, language, resource level)
    are what Node 1 already planned against, so a profile that repeats them adds
    nothing — but a profile MISSING them makes the feasibility factors go quiet
    on a lesson whose room is perfectly well described. They are therefore used
    only to fill gaps, and they arrive as `assumed` from `system`, because a
    default nobody confirmed is exactly that.
    """
    fields: dict[str, Field] = {}
    dropped: list[dict] = []

    def consider(name: str, raw_value: Any) -> None:
        value, provenance, origin, updated_at = _coerce(raw_value)

        if name not in factors_module.ALL_EVIDENCE_FIELDS:
            dropped.append({"field": name, "why": "no factor reads this field"})
            return
        if _empty(value):
            dropped.append({"field": name, "why": "empty"})
            return
        if provenance == UNKNOWN:
            dropped.append({"field": name, "why": "provenance is unknown"})
            return

        # The LOCAL-tier rule, applied here rather than at activation, so an
        # unverified community claim never even reaches the object the prompt is
        # built from. §5: "If data is missing — factor stays inactive"; an
        # assumed local fact IS missing data wearing a value.
        if provenance != VERIFIED and _is_local_only(name):
            dropped.append({
                "field": name,
                "why": f"local-context field is '{provenance}', and local context "
                       f"may only come from verified input"})
            return

        fields[name] = Field(name, value, provenance, origin, updated_at)

    for name, raw_value in (raw or {}).items():
        consider(name, raw_value)

    for name, value in _from_teacher_settings(teacher_settings or {}).items():
        if name not in fields:
            consider(name, value)

    return ContextProfile(fields, dropped=dropped,
                          school_id=school_id, class_id=class_id)


def _is_local_only(field_name: str) -> bool:
    """Is this field read ONLY by LOCAL-tier factors?

    "Only" matters. `household_skills` feeds both funds-of-knowledge (LOCAL) and
    family connection (LOCAL), so it is local-only and needs verification.
    `home_support_available` feeds family connection (LOCAL) AND the
    socioeconomic factor (BASELINE) — an assumed value there is still usable for
    the baseline judgement "do not set homework requiring an adult", and
    discarding it would lose a real feasibility signal to protect against a
    misuse the tier system already prevents.
    """
    readers = [f for f in factors_module.FACTORS if field_name in f.evidence]
    return bool(readers) and all(f.tier == factors_module.LOCAL for f in readers)


def _from_teacher_settings(settings: dict) -> dict:
    """The four Node 1 already planned against, mapped onto profile fields."""
    out: dict = {}
    if settings.get("classSize"):
        out["class_size"] = settings["classSize"]
    if settings.get("duration"):
        out["lesson_duration"] = settings["duration"]
    if settings.get("language"):
        out["medium_of_instruction"] = settings["language"]
    if settings.get("resourceLevel") is not None:
        out["resource_level"] = settings["resourceLevel"]
    if settings.get("teachingStyle"):
        out["teaching_style"] = settings["teachingStyle"]
    if settings.get("learningObjective"):
        out["learning_objective"] = settings["learningObjective"]
    return out


# ── Resource facts the gate needs as booleans ────────────────────────────────
#
# §10 requires "Required materials and technology must actually be available",
# and that check cannot be made against free text. These read the profile the
# way the gate needs to, in one place, so the gate and the prompt cannot disagree
# about whether the room has a projector.

# The physical facts about the room, as opposed to claims about the community.
#
# CITABLE BY ANY FACTOR, and that is the whole point of the list. They are
# CONSTRAINTS: a scaffolding adaptation that hands out a worksheet is unrunnable
# in a school with no paper just as surely as a resource adaptation is, and a
# classroom adaptation that puts something on the board needs to know there IS a
# board. Partitioning these per factor — which is what `activation.exposes` does
# for everything else — refused a good adaptation on the first real run: the
# model proposed drawing on the board, cited `has_board`, and the gate rejected
# it because that field sits on the resources factor rather than the classroom
# one.
#
# Everything NOT on this list stays partitioned. A local claim about the
# community is a claim, and which factor is entitled to make it is exactly the
# distinction the tier system exists to enforce.
ROOM_FACTS = frozenset({
    "materials_available", "has_board", "has_paper", "has_projector",
    "has_internet", "electricity", "devices_available", "classroom_space",
    "class_size", "lesson_duration", "resource_level", "seating",
})


def available_materials(profile: ContextProfile) -> set[str]:
    """Everything the room is recorded as having, lowercased.

    An EMPTY SET IS NOT "anything goes" — see `materials_recorded` below. The two
    have to be distinguishable or a school that has recorded nothing gets every
    material approved, which is the `Resource contradiction` failure in §18
    arriving through the check meant to prevent it.
    """
    names: set[str] = set()
    for field_name in ("materials_available",):
        value = profile.value(field_name)
        if isinstance(value, (list, tuple, set)):
            names |= {str(v).strip().lower() for v in value if str(v).strip()}
        elif isinstance(value, str):
            names |= {part.strip().lower() for part in value.split(",") if part.strip()}

    for field_name, implied in (("has_board", "board"), ("has_paper", "paper"),
                                ("has_projector", "projector"),
                                ("has_internet", "internet"),
                                ("electricity", "electricity")):
        if profile.value(field_name):
            names.add(implied)
    devices = profile.value("devices_available")
    if isinstance(devices, int) and devices > 0:
        names.add("device")
    return names


def materials_recorded(profile: ContextProfile) -> bool:
    """Did anybody actually say what this room has?"""
    return any(profile.has(name) for name in (
        "materials_available", "has_board", "has_paper", "has_projector",
        "has_internet", "electricity", "devices_available"))


def technology_available(profile: ContextProfile) -> bool:
    """Can a technology-dependent step be run at all?

    Electricity is ANDed rather than ORed with the device: a projector in a room
    with no power is a projector nobody can switch on, and §7 of the research
    framework is specific that digital access "can enable learning or become a
    barrier" depending on exactly this.
    """
    if profile.has("electricity") and not profile.value("electricity"):
        return False
    return bool(profile.value("devices_available")
                or profile.value("has_projector")
                or profile.value("has_internet"))
