"""How an answer is scored: ExaBase's adapter's scoring, its prompts and rules, which adapt the BEAM paper's.

- Event ordering: the answer's list is matched item by item to the topics in the answer key, and
  the two orders are compared with Kendall tau-b, normalized to 0-1.
- Every other category: the judge scores each rubric item 0, 0.5 or 1, and the question's score
  is their average.

The headline is the mean score over all questions (micro average). The summary also reports each
category's mean and the mean of those means (macro average).
"""

import math
import re
from collections import defaultdict

from scipy.stats import kendalltau

import exabase_prompts
import llm
from log import log
from dataset import Question


async def score_question(question: Question, answer: str) -> tuple[float, dict]:
    """The question's score between 0 and 1, and the judge's verdicts behind it."""
    if question.category == "event_ordering":
        reference = question.annotations.get("ordering_tested") or []
        if not reference and question.gold_answers:
            reference = list_items(question.gold_answers[0])
        if not reference:
            return 0.0, {"method": "event_ordering", "note": "the question has no reference order"}
        return await event_ordering_score(reference, list_items(answer))

    rubric = question.annotations.get("rubric") or []
    if not rubric and question.gold_answers:
        rubric = [f"LLM response should contain: {question.gold_answers[0]}"]
    if not rubric:
        return 0.0, {"method": "rubric", "note": "the question has no rubric"}
    verdicts = [await judge_rubric_item(question.text, answer, item) for item in rubric]
    score = sum(verdict["score"] for verdict in verdicts) / len(verdicts)
    return score, {"method": "rubric", "items": verdicts}


async def judge_rubric_item(question: str, answer: str, rubric_item: str) -> dict:
    prompt = exabase_prompts.rubric_item_prompt(question, answer, rubric_item)
    try:
        verdict = await llm.complete_json(prompt, exabase_prompts.RUBRIC_SCHEMA)
    except Exception as error:
        if llm.stops_the_run(error):
            raise
        # As ExaBase's adapter does: a verdict the judge could not give scores 0.
        return {"rubricItem": rubric_item, "score": 0.0, "error": str(error)[:500]}
    raw = float(verdict.get("score", 0))
    return {"rubricItem": rubric_item, "rawScore": raw, "score": round_rubric_score(raw),
            "reason": verdict.get("reason", "")}


def round_rubric_score(raw: float) -> float:
    """The judge may answer anything between 0 and 1: from 0.75 it counts as 1, from 0.25 as 0.5,
    and below that as 0."""
    return 1.0 if raw >= 0.75 else 0.5 if raw >= 0.25 else 0.0


def list_items(text: str) -> list[str]:
    """The lines of a numbered or bulleted list, without their numbers or bullets."""
    items = []
    for line in text.strip().splitlines():
        item = re.sub(r"^\s*(?:\d+[.)]\s*|[-*]\s*)", "", line).strip()
        if item:
            items.append(item)
    return items


async def same_item(reference_item: str, answer_item: str) -> bool:
    prompt = exabase_prompts.equivalence_prompt(reference_item, answer_item)
    try:
        verdict = await llm.complete_json(prompt, exabase_prompts.EQUIVALENCE_SCHEMA)
    except Exception as error:
        if llm.stops_the_run(error):
            raise
        # As ExaBase's adapter does: a match the judge could not confirm counts as different.
        log(f"an equivalence check failed ({str(error)[:120]}); counted as different")
        return False
    return verdict.get("answer", "").strip().upper().startswith("YES")


async def event_ordering_score(reference: list[str], answer_items: list[str]) -> tuple[float, dict]:
    if not reference or not answer_items:
        return 0.0, {"method": "event_ordering", "reference": reference, "answer": answer_items}

    # Each item of the answer takes the name of the first reference topic the judge calls the same,
    # so both lists speak of the same things. An item that matches nothing keeps its own words.
    matched: set[int] = set()
    renamed = []
    for item in answer_items:
        match = None
        for index, topic in enumerate(reference):
            if index not in matched and await same_item(topic, item):
                match = index
                break
        if match is None:
            renamed.append(item)
        else:
            renamed.append(reference[match])
            matched.add(match)

    return ordering_score(reference, renamed), {"method": "event_ordering", "reference": reference, "answer": renamed}


def ordering_score(reference: list[str], answer_order: list[str]) -> float:
    """How well two orders agree: Kendall tau-b from -1 to 1, mapped to 0-1 (so an unrelated
    order scores about 0.5, and a reversed one 0).

    Every topic is ranked in both orders; a topic missing from one order ranks after everything in it.
    """
    topics = list(dict.fromkeys(reference + answer_order))
    missing = len(topics) + 1

    def ranks(order: list[str]) -> list[int]:
        position = {topic: i + 1 for i, topic in enumerate(order)}
        return [position.get(topic, missing) for topic in topics]

    tau_b, _ = kendalltau(ranks(reference), ranks(answer_order), variant="b")
    return 0.0 if tau_b is None or math.isnan(tau_b) else float((tau_b + 1) / 2)


def summarize(rows: list[dict]) -> dict:
    """The run's scores: per category, and overall."""
    scores_by_category: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        scores_by_category[row["category"]].append(float(row["score"]))
    categories = {
        category: {"questions": len(scores), "mean": sum(scores) / len(scores)}
        for category, scores in sorted(scores_by_category.items())
    }
    scores = [float(row["score"]) for row in rows]
    return {
        "questions": len(scores),
        # Scored 0 because a recall or the answer failed, and rubric verdicts the judge could not give.
        "failedQuestions": sum(1 for row in rows if row.get("error")),
        "failedVerdicts": sum(1 for row in rows for item in (row.get("judge") or {}).get("items", [])
                              if item.get("error")),
        "microAverage": sum(scores) / len(scores) if scores else None,
        "macroAverage": sum(c["mean"] for c in categories.values()) / len(categories) if categories else None,
        "categories": categories,
    }
