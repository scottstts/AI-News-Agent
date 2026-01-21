"""Research agent runner service."""

import json
import re
from datetime import datetime
from pathlib import Path

from google.adk.runners import InMemoryRunner

from agent_core.agents import research_agent

RESEARCH_HISTORY_DIR = Path(__file__).resolve().parent.parent / "research_history"
RESEARCH_HISTORY_DIR.mkdir(exist_ok=True)


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

    runner = InMemoryRunner(agent=research_agent)
    user_message = "Research the latest AI development news from the past 24 hours as instructed."
    trace = await runner.run_debug(user_message)

    # Save trace
    trace_file = RESEARCH_HISTORY_DIR / f"trace_{timestamp}.json"
    trace_data = [event_to_dict(e) for e in trace]
    with trace_file.open("w", encoding="utf-8") as f:
        json.dump(trace_data, f, indent=2, default=str)

    # Save markdown results
    final_text = extract_final_text(trace)
    md_file = RESEARCH_HISTORY_DIR / f"research_{timestamp}.md"
    write_results_to_md(final_text, md_file, timestamp_readable)

    return md_file, trace_file


def get_latest_research_file() -> Path | None:
    """Get the most recent research markdown file."""
    md_files = list(RESEARCH_HISTORY_DIR.glob("research_*.md"))
    if not md_files:
        return None
    return max(md_files, key=lambda p: p.stat().st_mtime)
