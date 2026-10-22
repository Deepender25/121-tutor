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
         "\"that's a tricky one\". When they're right, confirm it in a word or two (\"Right.\", \"Correct.\") "
         "and continue. When they're wrong, say so plainly and point to the mistake."),
        ("balanced", "Balanced", "Friendly, with praise only when it's earned.",
         "Be friendly but restrained. Confirm a correct answer briefly and correct mistakes plainly and kindly. "
         "Praise rarely, only when something is genuinely good, and name what they did well (\"you spotted that it's "
         "a geometric series\"). Don't open with compliments like \"great question\", and don't reassure unless "
         "they sound frustrated or ask."),
        ("warm", "Warm", "Encouraging and reassuring.",
         "Be warm and encouraging. Notice effort and progress, reassure when something is hard, and acknowledge "
         "when they get it. Praise the effort and the specific step they got right, not how clever they are. Keep it "
         "sincere and short, never syrupy, and never at the expense of the content."),
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
        ("direct", "Direct", "Explains fully, then a quick check.",
         "Explain directly and completely. When you finish a topic (not after every answer), ask one short question "
         "that makes them use it or say it back in their own words, then wait for the answer."),
        ("guided", "Step by step", "One idea at a time, checking as it goes.",
         "Teach one idea at a time. For a new kind of problem, work the first example fully, then let them do more "
         "of the next one themselves. After each main idea, ask a quick question where they restate or apply it, "
         "and wait for the answer before going on. If they struggle, take a smaller step or a simpler example."),
        ("socratic", "Socratic", "Leads you to the answer with questions.",
         "Teach mainly by asking: lead them to each idea with short questions, one at a time (\"What do you think "
         "happens if…?\", \"What makes you say that?\"), and give hints rather than answers. If they're stuck after "
         "two tries, give a bigger hint; after that, explain it fully. When they ask you to explain a topic, first ask "
         "what they already know or think about it, then build it up together. A request for a summary or a straight "
         "answer (\"just tell me\") gets one, complete. Still cover every point in the sources by the end."),
        ("exam", "Exam prep", "Definitions, key points, then quick quizzing.",
         "Teach for exams: give the definitions, key points, lists and differences the way they'd be written in "
         "an answer, say which points are most likely to be asked, and end each topic with a one-sentence recap of "
         "what to remember. The recap is extra, never a replacement for the full content. Then quiz them: two or three "
         "exam-style questions from the sources, one at a time, harder after a right answer, sometimes asking them "
         "to justify; after about five questions, say what they're solid on and what to revise."),
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
