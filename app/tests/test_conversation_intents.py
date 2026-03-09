from app.services.conversation_intents import (
    ConversationCategory,
    PrescriptionSubtype,
    classify_main_intent,
    detect_prescription_subtype,
    detect_yes_no,
)


def test_classify_main_intent_presential():
    intent = classify_main_intent("quiero turno presencial")
    assert intent.pending_reason == "turno_presencial"
    assert intent.category == ConversationCategory.PRESENTIAL_APPOINTMENT


def test_classify_main_intent_virtual():
    intent = classify_main_intent("me gustaria atenderme online")
    assert intent.pending_reason == "turno_virtual"
    assert intent.category == ConversationCategory.VIRTUAL_APPOINTMENT


def test_classify_main_intent_human():
    intent = classify_main_intent("quiero hablar con una persona")
    assert intent.pending_reason == "humano"
    assert intent.category == ConversationCategory.HUMAN_HANDOFF


def test_classify_main_intent_ambiguous_turno():
    intent = classify_main_intent("quiero un turno")
    assert intent.pending_reason is None
    assert intent.requires_clarification is True


def test_detect_prescription_subtype_new():
    assert detect_prescription_subtype("receta nueva") == PrescriptionSubtype.NEW_PRESCRIPTION


def test_detect_prescription_subtype_expired():
    assert detect_prescription_subtype("se me vencio la receta") == PrescriptionSubtype.EXPIRED_PRESCRIPTION


def test_detect_prescription_subtype_order():
    assert detect_prescription_subtype("necesito una orden medica") == PrescriptionSubtype.MEDICAL_ORDER


def test_detect_yes_no_variants():
    assert detect_yes_no("dale") is True
    assert detect_yes_no("cancelar") is False
    assert detect_yes_no("quizas") is None
