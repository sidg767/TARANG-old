import json
import os
import re

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv(
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../../../.env"
        )
    )
)


# ============================================================
# ROUTER
# ============================================================

router = APIRouter(
    tags=["AI Copilot"]
)


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================

class CopilotRequest(BaseModel):
    query: str


class CopilotAction(BaseModel):
    type: str
    value: str | float | int | bool | None = None


class CopilotResponse(BaseModel):
    answer: str
    requires_confirmation: bool = False
    actions: list[CopilotAction] = Field(default_factory=list)


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are TARANG AI Copilot.

TARANG is a 3D ocean visualization platform.

Your job is to understand natural language and convert
visualization requests into TARANG actions.

IMPORTANT:
Preserve every number supplied by the user exactly.

10 means 10.
20 means 20.
30 means 30.
45 means 45.
12.5 means 12.5.
250 means 250.

NEVER replace a user number with an example number.

============================================================
AVAILABLE ACTIONS
============================================================

1. set_variable

Allowed values:

temperature
salinity

Example:

{
  "type": "set_variable",
  "value": "salinity"
}


2. set_depth

Use for ANY requested depth.

Examples:

10m -> 10
20m -> 20
30m -> 30
100 meters -> 100
250 m -> 250

Example:

{
  "type": "set_depth",
  "value": 30
}


3. set_render_mode

Allowed values:

slice
volume
isosurface

Example:

{
  "type": "set_render_mode",
  "value": "isosurface"
}


4. set_isosurface

Use when the user gives an isosurface threshold.

The threshold is dynamic.

Examples:

threshold 10 -> 10
threshold 20 -> 20
threshold 30 -> 30
threshold 12.5 -> 12.5

Example:

{
  "type": "set_isosurface",
  "value": 12.5
}


5. toggle_layer

Allowed values:

slice
volume
isosurface
markers
vectors

Example:

{
  "type": "toggle_layer",
  "value": "markers"
}

============================================================
MULTIPLE ACTIONS
============================================================

A request can contain multiple changes.

Extract ALL applicable actions.

Example:

User:
show salinity at 30m

Return:

{
  "answer": "I can switch to salinity and show it at 30 m. Shall I apply these changes?",
  "requires_confirmation": true,
  "actions": [
    {
      "type": "set_variable",
      "value": "salinity"
    },
    {
      "type": "set_depth",
      "value": 30
    }
  ]
}

Example:

User:
show temperature at 250m with isosurface threshold 12.5

Return:

{
  "answer": "I can show temperature at 250 m with an isosurface threshold of 12.5. Shall I apply these changes?",
  "requires_confirmation": true,
  "actions": [
    {
      "type": "set_variable",
      "value": "temperature"
    },
    {
      "type": "set_depth",
      "value": 250
    },
    {
      "type": "set_render_mode",
      "value": "isosurface"
    },
    {
      "type": "set_isosurface",
      "value": 12.5
    }
  ]
}

Example:

User:
show salinity at 10m with threshold 20

Return:

{
  "answer": "I can switch to salinity at 10 m and apply an isosurface threshold of 20. Shall I apply these changes?",
  "requires_confirmation": true,
  "actions": [
    {
      "type": "set_variable",
      "value": "salinity"
    },
    {
      "type": "set_depth",
      "value": 10
    },
    {
      "type": "set_render_mode",
      "value": "isosurface"
    },
    {
      "type": "set_isosurface",
      "value": 20
    }
  ]
}

Example:

User:
set salinity depth 40m threshold 12.5 and show markers

Return:

{
  "answer": "I can switch to salinity at 40 m, apply a threshold of 12.5, and show markers. Shall I apply these changes?",
  "requires_confirmation": true,
  "actions": [
    {
      "type": "set_variable",
      "value": "salinity"
    },
    {
      "type": "set_depth",
      "value": 40
    },
    {
      "type": "set_render_mode",
      "value": "isosurface"
    },
    {
      "type": "set_isosurface",
      "value": 12.5
    },
    {
      "type": "toggle_layer",
      "value": "markers"
    }
  ]
}

============================================================
NATURAL LANGUAGE
============================================================

Understand all of these:

show salinity at 20m
show salinity at 20 meters
display salinity at depth 20
give me salinity at 20 m

All mean:

set_variable = salinity
set_depth = 20


These all mean isosurface:

threshold 10
set threshold to 10
use 10 threshold
isosurface at 10
show the 10 isosurface

Return:

set_render_mode = isosurface
set_isosurface = 10

============================================================
CONFIRMATION
============================================================

Every visualization change requires confirmation.

requires_confirmation must be true.

The answer must ask whether to apply the changes.

============================================================
NORMAL QUESTIONS
============================================================

If the user asks an informational question and does not request
a visualization change:

requires_confirmation = false
actions = []

Example:

What is ocean salinity?

Return:

{
  "answer": "Ocean salinity is the concentration of dissolved salts in seawater, commonly expressed using Practical Salinity Units (PSU).",
  "requires_confirmation": false,
  "actions": []
}

============================================================
CANCEL
============================================================

If the user says:

no
no thanks
cancel
don't do it
leave it
never mind

Return:

{
  "answer": "No problem. I'll leave the visualization unchanged. What would you like to explore next?",
  "requires_confirmation": false,
  "actions": []
}

============================================================
UNRELATED QUESTIONS
============================================================

If the question is unrelated to ocean visualization or ocean
data, politely explain that you are TARANG's ocean-focused AI
Copilot.

Do not create actions.

============================================================
OUTPUT
============================================================

Return ONLY valid JSON.

Do not use markdown.

Do not use code fences.

Always use exactly:

{
  "answer": "string",
  "requires_confirmation": true or false,
  "actions": []
}
"""


# ============================================================
# JSON EXTRACTION
# ============================================================

def extract_json(raw: str) -> str:
    """
    Extract the first valid JSON object from the model response.
    Handles markdown fences and extra text.
    """

    if not raw:
        raise ValueError("Empty model response")

    raw = raw.strip()

    # Remove markdown code fences
    raw = re.sub(
        r"^```(?:json)?\s*",
        "",
        raw,
        flags=re.IGNORECASE
    )

    raw = re.sub(
        r"\s*```$",
        "",
        raw
    )

    raw = raw.strip()

    # First try the entire response
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Find first JSON object
    start = raw.find("{")

    if start == -1:
        raise ValueError(
            f"No JSON object found in model response: {raw[:500]}"
        )

    # Find matching closing brace
    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(raw)):

        char = raw[i]

        if escape:
            escape = False
            continue

        if char == "\\":
            escape = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                candidate = raw[start:i + 1]

                # Validate JSON
                json.loads(candidate)

                return candidate

    raise ValueError(
        f"Incomplete JSON response: {raw[:500]}"
    )


# ============================================================
# ALLOWED ACTIONS
# ============================================================

ALLOWED_ACTIONS = {
    "set_variable",
    "set_depth",
    "set_render_mode",
    "set_isosurface",
    "toggle_layer",
}


# ============================================================
# OPENROUTER
# ============================================================

async def ask_openrouter(query: str) -> dict:

    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENROUTER_API_KEY is not configured"
        )

    model = os.getenv(
        "OPENROUTER_MODEL",
        "nvidia/nemotron-3-nano-30b-a3b:free"
    )

    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": "TARANG AI Copilot",
        }
    )

    try:

        response = await client.chat.completions.create(
            model=model,

            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": query
                }
            ],

            temperature=0,

            max_tokens=1000,
        )

    except Exception as e:

        print()
        print("==========================================")
        print("OPENROUTER REQUEST FAILED")
        print(repr(e))
        print("==========================================")
        print()

        raise HTTPException(
            status_code=502,
            detail=f"OpenRouter request failed: {e!s}"
        )

    if not response.choices:

        raise HTTPException(
            status_code=502,
            detail="OpenRouter returned no choices"
        )

    raw = response.choices[0].message.content

    if not raw:

        raise HTTPException(
            status_code=502,
            detail="OpenRouter returned an empty response"
        )

    print()
    print("========== OPENROUTER RAW RESPONSE ==========")
    print(raw)
    print("==============================================")
    print()

    try:

        cleaned = extract_json(raw)

        data = json.loads(cleaned)

    except Exception as e:

        print()
        print("========== JSON PARSING FAILED ==========")
        print(repr(e))
        print("RAW RESPONSE:")
        print(raw)
        print("=========================================")
        print()

        raise HTTPException(
            status_code=502,
            detail="AI Copilot returned an invalid response"
        )

    if not isinstance(data, dict):

        raise HTTPException(
            status_code=502,
            detail="AI Copilot response is not a JSON object"
        )

    return data


# ============================================================
# COPILOT ENDPOINT
# ============================================================

@router.post(
    "/copilot",
    response_model=CopilotResponse
)
async def copilot(
    request: CopilotRequest
):

    query = request.query.strip()

    if not query:

        raise HTTPException(
            status_code=400,
            detail="Query cannot be empty"
        )

    # Ask AI
    data = await ask_openrouter(query)

    # Answer
    answer = str(
        data.get(
            "answer",
            "I couldn't generate a response."
        )
    )

    # Confirmation
    requires_confirmation = bool(
        data.get(
            "requires_confirmation",
            False
        )
    )

    # Actions
    raw_actions = data.get(
        "actions",
        []
    )

    actions = []

    if isinstance(raw_actions, list):

        for item in raw_actions:

            if not isinstance(item, dict):
                continue

            action_type = item.get("type")
            action_value = item.get("value")

            if action_type not in ALLOWED_ACTIONS:

                print(
                    "Ignoring unsupported action:",
                    action_type
                )

                continue

            actions.append(
                CopilotAction(
                    type=action_type,
                    value=action_value
                )
            )

    # ========================================================
    # CONSISTENCY RULES
    # ========================================================

    # No confirmation means no actions
    if not requires_confirmation:
        actions = []

    # Confirmation without actions makes no sense
    if requires_confirmation and not actions:
        requires_confirmation = False

    result = CopilotResponse(
        answer=answer,
        requires_confirmation=requires_confirmation,
        actions=actions
    )

    print()
    print("========== TARANG COPILOT ==========")
    print(result.model_dump_json())
    print("====================================")
    print()

    return result