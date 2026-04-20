from __future__ import annotations

LANG_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish"}

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
        "Output only the summary itself, with no preamble, headers, or meta-commentary.\n\n"
        f"Transcript:\n{transcript}"
    )
