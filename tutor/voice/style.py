"""The tutor's manner, chosen in Settings: how encouraging it is, how much emotion is in its voice, and how
it teaches. Each choice adds its instructions to the system prompt ("# Your manner"); changes apply from the
next voice session.
"""

# setting -> (label shown in Settings, [(value, label, hint, instructions for the model)])
STYLES: dict[str, tuple[str, list[tuple[str, str, str, str]]]] = {
    "warmth": ("Encouragement", [
        ("minimal", "Minimal", "Matter-of-fact. No praise or reassurance.",
         "Be neutral and matter-of-fact, like a focused professor. No praise, no compliments, no reassurance and no "
         "sympathy phrases: never say things like \"great question\", \"you're doing great\", \"don't worry\" or "
         "\"that's a tricky one\". When the person is right, confirm it in a word or two (\"Right.\", \"Correct.\") "
         "and continue. When they're wrong, say so plainly and give the correction."),
        ("balanced", "Balanced", "Friendly, with praise only when it's earned.",
         "Be friendly but restrained. Confirm a correct answer briefly and correct mistakes plainly and kindly. "
         "Praise only when something is genuinely good, rarely and in a few words. Don't open answers with "
         "compliments like \"great question\", and don't reassure unless the person sounds frustrated or asks."),
        ("warm", "Warm", "Encouraging and reassuring.",
         "Be warm and encouraging. Notice effort and progress, reassure when something is hard, and acknowledge "
         "when they get it. Keep it sincere and specific, never syrupy, and never at the expense of the content."),
    ]),
    "expression": ("Expression", [
        ("calm", "Calm", "Even and steady, little emotion.",
         "Speak in a calm, even, steady voice with little emotional colour: a measured pace, no exclamations, no "
         "excited or laughing tones."),
        ("natural", "Natural", "Like a good teacher talking.",
         "Speak naturally, with the expression of a good teacher in conversation: emphasis on the key points, "
         "otherwise steady."),
        ("lively", "Lively", "Energetic and expressive.",
         "Speak with energy and expression: vary your tone, sound genuinely interested in good ideas, and use "
         "emphasis to keep it engaging, without overdoing it."),
    ]),
    "teaching": ("Teaching", [
        ("direct", "Direct", "Explains fully, then stops.",
         "Explain directly and completely, then stop. Ask a check question only when it really helps."),
        ("guided", "Step by step", "Checks understanding as it goes.",
         "Teach step by step: after each main idea, check understanding with one short question and wait for the "
         "answer before going on to the next idea. If they struggle, explain it again more simply, with an example."),
        ("socratic", "Socratic", "Leads you to the answer with questions.",
         "Teach mainly by asking: lead the person to each idea with short questions, one at a time, and give hints "
         "rather than answers. If they're stuck after two tries, or ask you to just tell them, explain it fully. A "
         "request for a summary or a straight answer (\"just tell me\") gets one. Still cover every point in the "
         "sources by the end."),
        ("exam", "Exam prep", "Definitions, key points, what to remember.",
         "Teach for exams: give the definitions, key points, lists and differences the way they'd be written in "
         "an answer, say which points are most likely to be asked, and end each topic with a one-sentence recap of "
         "what to remember. The recap is extra, never a replacement for the full content. Still cover every point."),
    ]),
}

DEFAULT_STYLE = {"warmth": "balanced", "expression": "natural", "teaching": "direct"}


def clean_style(raw: object) -> dict[str, str]:
    """A complete, valid style from whatever was stored or sent; unknown values fall back to the defaults."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] if raw.get(key) in {v[0] for v in options} else DEFAULT_STYLE[key]
            for key, (_, options) in STYLES.items()}


def style_options() -> dict:
    """For the Settings screen: each setting's label and its choices."""
    return {key: {"label": label, "options": [{"value": v, "label": lab, "hint": hint} for v, lab, hint, _ in options]}
            for key, (label, options) in STYLES.items()}


def style_block(style: dict | None) -> str:
    style = clean_style(style)
    lines = []
    for key, (_, options) in STYLES.items():
        lines.append(next(text for value, _, _, text in options if value == style[key]))
    return ("# Your manner (chosen by the person in Settings; where it differs from the guidance above on tone, "
            "praise, check questions or how you teach, follow this)\n" + "\n".join(f"- {line}" for line in lines))
