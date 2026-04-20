from __future__ import annotations

LANG_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish"}

PLAIN_TEXT_RULES = (
    "Formatting rules:\n"
    "- Output PLAIN TEXT only. No markdown, no asterisks, no ## headers.\n"
    "- Do NOT use ** or * for emphasis.\n"
    "- For bullet points, start each line with '• ' (bullet character, then space).\n"
    "- Separate sections with a blank line."
)

MODE_INSTRUCTIONS = {
    "short": "Summarize the following voice message in 1-2 sentences capturing only the core point.",
    "medium": "Summarize the following voice message as 3-5 concise bullet points covering the key points.",
    "full": (
        "Summarize the following voice message. Start with a one-sentence overview, "
        "then list the main points and any concrete details (names, dates, numbers, requests, "
        "decisions) as bullets. Be faithful to the speaker — do not invent facts or add opinions."
    ),
}


def build_summary_prompt(transcript: str, mode: str, target_lang: str) -> str:
    instruction = MODE_INSTRUCTIONS[mode]
    if target_lang == "auto":
        lang_line = "Write the summary in the SAME language as the transcript."
    else:
        lang_line = f"Write the summary in {LANG_NAMES[target_lang]}, regardless of the transcript's language."
    return (
        f"{instruction}\n{lang_line}\n"
        "Output only the summary itself, with no preamble or meta-commentary.\n\n"
        f"{PLAIN_TEXT_RULES}\n\n"
        f"Transcript:\n{transcript}"
    )


def build_translation_prompt(transcript: str, target_lang: str) -> str:
    lang_name = LANG_NAMES[target_lang]
    return (
        f"Translate the following transcript into {lang_name}.\n"
        "Preserve every detail — do not summarize, shorten, paraphrase, or omit anything. "
        "Keep the speaker's tone. Filler words can be dropped for readability, "
        "but all substantive content must remain.\n"
        "Output only the translated text, with no preamble or meta-commentary.\n\n"
        f"{PLAIN_TEXT_RULES}\n\n"
        f"Transcript:\n{transcript}"
    )
