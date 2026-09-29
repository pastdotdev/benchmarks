"""ExaBase's published BEAM prompts, unchanged.

Source: ExaBase's BEAM adapter, https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX
SHA-256 of that file: 584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43

Credit for the answer and judge prompts belongs to ExaBase. The ExaBase-derived
portions are excluded from this repository's MIT license; this attribution does
not relicense them. See NOTICE.md for attribution and source details.

The function bodies below are copied from it unchanged, so our numbers compare with
other published BEAM results:

- answer_prompt: the answering prompt. For each question category it adds guidance, and part of
  that guidance comes from the dataset's own annotations for the question (the topics to order,
  the rubric items a summary should cover, time points, the preference or instruction under test,
  why a question may be unanswerable). That is ExaBase's methodology, reproduced as published.
- rubric_item_prompt: ExaBase's version of the BEAM paper's judge, one rubric item at a time.
- equivalence_prompt: whether two listed items name the same thing, for event ordering.

`python verify.py --prompts` downloads ExaBase's file and checks these bodies against it.
"""

SOURCE_URL = "https://fabric.so/p/beam-3VjBcqEVRofZyA5CeazeX"
SOURCE_SHA256 = "584b672d957f3e6f75e96f881ec31da22e1039da1df5fbedb53dc6a9145cfc43"


def answer_prompt(query: str, context: str, cat: str, meta: dict) -> str:
    """The prompt the answer model reads: guidance for the category, the question, the context."""
    category_guidance = ""
    if cat == "abstention":
        why = meta.get("why_unanswerable", "")
        category_guidance = (
            "IMPORTANT — ABSTENTION TASK: This question may be about something NOT "
            "mentioned in the conversation. If the context does not explicitly contain "
            "the requested information, you MUST respond with: "
            "\"Based on the provided chat, there is no information related to [topic].\" "
            "Do NOT fabricate, infer, or guess. Only answer if the information is "
            "directly present in the retrieved context."
            + (f"\n\nHint about why this may be unanswerable: {why}" if why else "")
        )
    elif cat == "event_ordering":
        topics = meta.get("ordering_tested", [])
        topics_str = "\n".join(f"  - {t}" for t in topics) if topics else ""
        category_guidance = (
            "IMPORTANT — EVENT ORDERING TASK: The following specific topics need to be "
            "listed in the order they were FIRST mentioned in the conversation.\n"
            + (f"\nTopics to order:\n{topics_str}\n" if topics_str else "")
            + "\nFor each topic, find the FIRST time it was mentioned (use Turn IDs and "
            "timestamps as ordering cues). List ONLY these topics in chronological order. "
            "Number each item. Do NOT add extra topics not listed above."
        )
    elif cat == "contradiction_resolution":
        category_guidance = (
            "IMPORTANT — CONTRADICTION TASK: The conversation contains contradictory "
            "statements. Identify both contradictory statements and explicitly note the "
            "contradiction. Do not give a definitive answer — instead say what was "
            "claimed at different points."
        )
    elif cat == "knowledge_update":
        category_guidance = (
            "IMPORTANT — KNOWLEDGE UPDATE TASK: Information was updated during the "
            "conversation. Report ONLY the most recent value. If you see older "
            "conflicting info, note the update."
        )
    elif cat == "temporal_reasoning":
        time_pts = meta.get("time_points", [])
        calc = meta.get("calculation_required", "")
        guidance = (
            "IMPORTANT — TEMPORAL REASONING TASK: Calculate the exact time duration. "
            "Show your arithmetic step by step."
        )
        if time_pts:
            guidance += f"\n\nKey time points from context: {'; '.join(time_pts)}"
        if calc:
            guidance += f"\n\nCalculation hint: {calc}"
        category_guidance = guidance
    elif cat == "preference_following":
        pref = meta.get("preference_being_tested", "")
        category_guidance = (
            "IMPORTANT — PREFERENCE FOLLOWING TASK: The user has stated a specific "
            "preference earlier in the conversation. Your answer MUST respect and comply "
            "with that stated preference."
            + (f"\n\nUser's preference (from conversation): {pref}" if pref else "")
        )
    elif cat == "instruction_following":
        instr = meta.get("instruction_being_tested", "")
        indicators = meta.get("compliance_indicators", [])
        guidance = (
            "IMPORTANT — INSTRUCTION FOLLOWING TASK: The user gave a specific formatting "
            "or style instruction earlier in the conversation. Follow it exactly."
        )
        if instr:
            guidance += f"\n\nInstruction to follow: {instr}"
        if indicators:
            guidance += f"\n\nCompliance indicators: {', '.join(indicators)}"
        category_guidance = guidance
    elif cat == "summarization":
        rubric_items = meta.get("rubric", [])
        # Strip the "LLM response should contain: " prefix for cleaner hints
        hints = []
        for r in rubric_items:
            hint = r.split(": ", 1)[-1] if ": " in r else r
            hints.append(hint)
        hints_str = "\n".join(f"  - {h}" for h in hints) if hints else ""
        category_guidance = (
            "IMPORTANT — SUMMARIZATION TASK: Provide a comprehensive chronological "
            "summary. Be specific about dates, versions, and key technical decisions.\n"
            + (f"\nMake sure your summary covers these key aspects:\n{hints_str}" if hints_str else "")
        )
    elif cat == "multi_session_reasoning":
        category_guidance = (
            "IMPORTANT — MULTI-SESSION REASONING TASK: The answer requires "
            "combining facts from multiple parts of the conversation.\n\n"
            "Steps:\n"
            "1. Before giving any count or total, FIRST list each distinct item "
            "you found, with its Turn number or source reference.\n"
            "2. Review your list — merge items that are the same thing described "
            "differently. Do NOT split a single item into sub-items or count "
            "variations of the same thing separately.\n"
            "3. Only then compute your count or total from the deduplicated list.\n"
            "4. If the question asks for a total amount, show the arithmetic "
            "(e.g. $X + $Y + $Z = $Total).\n\n"
            "Count only items the user explicitly mentioned — do not infer or "
            "generalize beyond what is stated."
        )
    elif cat == "information_extraction":
        category_guidance = (
            "IMPORTANT — INFORMATION EXTRACTION TASK: Answer with the specific "
            "fact, value, or detail as it was originally stated at the point in "
            "the conversation the question refers to. Use Turn numbers to locate "
            "the relevant statement.\n\n"
            "If a value was later updated or changed, report what was stated at "
            "the time the question asks about (typically the FIRST mention), NOT "
            "the updated value. If the question says 'did I say' or 'did I "
            "mention', report the original statement."
        )

    return f"""You are a helpful assistant answering questions based on a long conversation history.
Answer the question using ONLY information found in the retrieved context below.

{category_guidance}

Question: {query}

Retrieved Context:
{context}

Answer:"""


def rubric_item_prompt(query: str, answer: str, rubric_item: str) -> str:
    prompt = f"""You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## QUESTION:
{query}

## LLM RESPONSE:
{answer}

## RUBRIC CRITERION:
{rubric_item}

## SCORING:
- **1.0** = Fully satisfied: The response clearly and completely addresses this rubric criterion.
- **0.5** = Partially satisfied: The response addresses this criterion but is incomplete, vague, or only partially correct.
- **0.0** = Not satisfied: The response does not address this criterion at all, or is incorrect.

Evaluate the response against ONLY this specific rubric criterion. Provide your score and a brief reason."""
    return prompt


# The judge answers with a score and a one-line reason.
RUBRIC_SCHEMA = {
    "properties": {
        "score": {"type": "number", "description": "Score: 0.0, 0.5, or 1.0"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


def equivalence_prompt(ref_item: str, sys_item: str) -> str:
    prompt = f"""Do these two items refer to the same event, topic, or concept? Answer only YES or NO.

Item A: {ref_item}
Item B: {sys_item}

Answer (YES or NO):"""
    return prompt


EQUIVALENCE_SCHEMA = {
    "properties": {"answer": {"type": "string", "description": "YES or NO"}},
    "required": ["answer"],
}
