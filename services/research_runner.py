"""Research agent runner service."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent_core.agents import research_agent

APP_NAME = "ai_news_research"

RESEARCH_HISTORY_DIR = Path(__file__).resolve().parent.parent / "research_history"
RESEARCH_HISTORY_DIR.mkdir(exist_ok=True)

# File for real-time token usage tracking (read by get_token_budget_info tool)
TOKEN_USAGE_FILE = RESEARCH_HISTORY_DIR / "current_token_usage.json"


def update_token_usage(prompt_tokens: int, total_tokens: int) -> None:
    """Write current token usage to file for the agent tool to read."""
    data = {
        "prompt_token_count": prompt_tokens,
        "total_token_count": total_tokens,
        "updated_at": datetime.now().isoformat(),
    }
    TOKEN_USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")


def clear_token_usage() -> None:
    """Clear token usage file at start of new run."""
    if TOKEN_USAGE_FILE.exists():
        TOKEN_USAGE_FILE.unlink()


def event_to_dict(event):
    """Convert ADK Event to serializable dict."""
    if hasattr(event, "model_dump"):
        return event.model_dump()
    elif hasattr(event, "dict"):
        return event.dict()
    elif hasattr(event, "__dict__"):
        return event.__dict__
    return str(event)


def extract_final_text(trace: list) -> str:
    """Extract final output text from trace events."""
    for event in reversed(trace):
        if hasattr(event, "content"):
            content = event.content
            if hasattr(content, "parts"):
                for part in content.parts:
                    if hasattr(part, "text") and part.text:
                        return part.text
            elif hasattr(content, "text") and content.text:
                return content.text
        if hasattr(event, "model_dump"):
            d = event.model_dump()
            if "content" in d and d["content"]:
                c = d["content"]
                if "parts" in c:
                    for p in c["parts"]:
                        if "text" in p and p["text"]:
                            return p["text"]
    return "No final text found in trace."


def extract_json_from_text(text: str) -> dict | None:
    """Extract JSON object from text that may contain markdown code blocks."""
    # Try to find JSON in code blocks first
    json_block_pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
    match = re.search(json_block_pattern, text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    json_pattern = r"\{[\s\S]*\"news\"[\s\S]*\}"
    match = re.search(json_pattern, text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def format_research_to_md(data: dict, timestamp: str) -> str:
    """Format parsed research JSON into readable markdown."""
    lines = [
        "# AI News Research Results",
        "",
        f"**Generated:** {timestamp}",
        "",
    ]

    if data.get("comments"):
        lines.extend([
            "## Research Notes",
            "",
            data["comments"],
            "",
        ])

    news_items = data.get("news", [])
    lines.extend([
        f"## News Items ({len(news_items)} found)",
        "",
    ])

    if not news_items:
        lines.append("*No news items found.*")
    else:
        for i, item in enumerate(news_items, 1):
            title = item.get("title", "Untitled")
            body = item.get("body", "No content.")
            sources = item.get("sources", [])

            lines.extend([
                f"### {i}. {title}",
                "",
                body,
                "",
            ])

            if sources:
                lines.append("**Sources:**")
                for source in sources:
                    lines.append(f"- {source}")
                lines.append("")

            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def write_results_to_md(text: str, output_path: Path, timestamp: str) -> None:
    """Write extracted text to markdown file, parsing JSON if possible."""
    parsed = extract_json_from_text(text)

    if parsed and "news" in parsed:
        formatted = format_research_to_md(parsed, timestamp)
        output_path.write_text(formatted, encoding="utf-8")
    else:
        content = f"# Research Agent Run\n\n**Generated:** {timestamp}\n\n{text}"
        output_path.write_text(content, encoding="utf-8")


async def run_research_agent() -> tuple[Path, Path]:
    """
    Run the research agent and save results.

    Returns:
        Tuple of (md_file_path, trace_file_path)
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_readable = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Clear any stale token usage from previous run
    clear_token_usage()

    # Create session service and runner
    session_service = InMemorySessionService()
    runner = Runner(
        agent=research_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    user_id = "research_system"
    session_id = f"research_{timestamp}_{uuid.uuid4().hex[:8]}"

    # Create session before running (required for run_async)
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    user_message = types.Content(
        role="user",
        parts=[types.Part(text="Research the latest AI development news from the past 24 hours as instructed.")],
    )

    # Collect events while streaming and updating token usage
    trace = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_message,
    ):
        trace.append(event)

        # Update token usage file after each event with usage_metadata
        if hasattr(event, "usage_metadata") and event.usage_metadata:
            usage = event.usage_metadata
            prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
            total_tokens = getattr(usage, "total_token_count", 0) or 0
            if prompt_tokens > 0:
                update_token_usage(prompt_tokens, total_tokens)

    # Save trace
    trace_file = RESEARCH_HISTORY_DIR / f"trace_{timestamp}.json"
    trace_data = [event_to_dict(e) for e in trace]
    with trace_file.open("w", encoding="utf-8") as f:
        json.dump(trace_data, f, indent=2, default=str)

    # Save markdown results
    final_text = extract_final_text(trace)
    md_file = RESEARCH_HISTORY_DIR / f"research_{timestamp}.md"
    write_results_to_md(final_text, md_file, timestamp_readable)

    # Clean up token usage file after run completes
    clear_token_usage()

    return md_file, trace_file


def get_latest_research_file() -> Path | None:
    """Get the most recent research markdown file."""
    md_files = list(RESEARCH_HISTORY_DIR.glob("research_*.md"))
    if not md_files:
        return None
    return max(md_files, key=lambda p: p.stat().st_mtime)
