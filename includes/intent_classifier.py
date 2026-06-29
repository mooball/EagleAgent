"""Reusable intent classifier using Gemini Flash with structured output.

Usage:
    from includes.intent_classifier import classify_intent

    intent = await classify_intent(
        message="find me suppliers for this rfq",
        directions=[
            {"id": "PIPELINE_RUN", "description": "User wants to find, source, or search for suppliers"},
            {"id": "QUESTION", "description": "User is asking a question about the RFQ or its items"},
        ],
        context="User is viewing RFQ-2026-0039 with 8 items, pipeline_stage=unprocessed",
    )
    # intent == "PIPELINE_RUN"
"""

import asyncio
import enum
import logging
from typing import Optional

from google import genai
from google.genai import types

from config.settings import Config

logger = logging.getLogger(__name__)

# Use the fast model configured for the supervisor (gemini-2.0-flash)
_CLASSIFIER_MODEL = Config.SUPERVISOR_MODEL


async def classify_intent(
    message: str,
    directions: list[dict],
    context: str = "",
) -> str:
    """Classify user intent into one of the predefined directions.

    Args:
        message: The user's message text.
        directions: List of dicts with "id" and "description" keys.
                   An "OTHER" option is always added automatically.
        context: Optional context about the current state (RFQ info, pipeline stage, etc.)

    Returns:
        The "id" of the matched direction, or "OTHER".
    """
    if not directions:
        return "OTHER"

    # Build the direction list for the prompt
    direction_lines = []
    valid_ids = []
    for d in directions:
        direction_lines.append(f"- {d['id']}: {d['description']}")
        valid_ids.append(d["id"])
    valid_ids.append("OTHER")
    direction_text = "\n".join(direction_lines)

    # Build the enum for structured output
    DirectionEnum = enum.Enum("DirectionEnum", {id_: id_ for id_ in valid_ids})

    prompt = f"""Classify the user's intent into exactly one of the following directions.

Directions:
{direction_text}
- OTHER: None of the above directions match

{f"Context: {context}" if context else ""}

User message: "{message}"

Which direction best matches the user's intent? Reply with exactly one direction ID."""

    try:
        client = genai.Client()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_CLASSIFIER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="text/x.enum",
                response_schema=DirectionEnum,
                temperature=0.0,
            ),
        )

        result = (response.text or "").strip()
        if result in valid_ids:
            logger.info(f"Intent classifier: '{message[:60]}' → {result}")
            return result

        # Fallback: try to match partial response
        for id_ in valid_ids:
            if id_ in result:
                logger.info(f"Intent classifier (partial match): '{message[:60]}' → {id_}")
                return id_

        logger.warning(f"Intent classifier: unexpected response '{result}', defaulting to OTHER")
        return "OTHER"

    except Exception as e:
        logger.error(f"Intent classifier failed: {e}")
        return "OTHER"
