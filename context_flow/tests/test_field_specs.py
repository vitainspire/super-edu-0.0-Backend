"""`field_specs` must describe exactly the fields the registry reads.

The form in `studio.py` is generated from `factors.FACTORS`, so a field the
registry gains and `field_specs` has not heard of still appears — as a plain
text box with a prettified label. That fallback is deliberate, and it is also
exactly the kind of quiet degradation nobody notices, so the parity is asserted
here rather than left to somebody spotting a shabby label.

This file replaced a form that named ELEVEN fields inline. The other forty-five
could not be entered at all, which meant most of the twenty factors could never
activate no matter what a school knew about its own classroom.
"""
from context_flow import factors, field_specs


def _registry_fields() -> set:
    return {e for f in factors.FACTORS for e in f.evidence}


def test_every_registry_field_has_a_spec():
    missing = _registry_fields() - set(field_specs.SPECS)
    assert not missing, f"no widget described for: {sorted(missing)}"


def test_no_spec_describes_a_field_nobody_reads():
    extra = set(field_specs.SPECS) - _registry_fields()
    assert not extra, f"described but unread: {sorted(extra)}"


def test_the_two_gate_factors_ask_for_nothing():
    # Equity and safety are constraints the finished plan is checked against,
    # never adaptation generators — a form field for either would be asking a
    # school to supply evidence the gate does not take.
    gates = [f for f in factors.FACTORS if f.tier == factors.GATE]
    assert len(gates) == 2
    assert all(not f.evidence for f in gates)


def test_an_unknown_field_still_gets_a_usable_widget():
    spec = field_specs.spec_for("some_new_field")
    assert spec.label == "Some new field"
    assert spec.kind == field_specs.TEXT


def test_no_spec_ships_a_default_value_for_a_local_field():
    """A pre-filled community field is the §18 failure mode in form clothing.

    Placeholders are fine — they are grey text showing the shape of an answer.
    A DEFAULT is data nobody entered, and by the time it reaches the model it is
    indistinguishable from something a school confirmed.
    """
    from context_flow.profile import _is_local_only
    for name, spec in field_specs.SPECS.items():
        if _is_local_only(name):
            assert spec.default == 0, f"{name} ships a default"
            assert spec.kind in (field_specs.TEXT, field_specs.AREA), name
