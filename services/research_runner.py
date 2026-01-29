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
from .cleanup import (
    RESEARCH_HISTORY_DIR,
    cleanup_failed_run,
    cleanup_previous_run,
    clear_agent_notes,
    clear_token_usage,
    update_token_usage,
)

APP_NAME = "ai_news_research"


def event_to_dict(event):
    """Convert ADK Event to serializable dict."""
    if hasattr(event, "model_dump"):
        return event.model_dump()
    elif hasattr(event, "dict"):
        return event.dict()
    elif hasattr(event, "__dict__"):
        return event.__dict__
    return str(event)


class TraceWriter:
    """
    Incrementally writes trace events to a JSON file to minimize memory usage.
    Writes events as they arrive instead of accumulating in memory.
    """

    def __init__(self, trace_file: Path):
        self.trace_file = trace_file
        self.event_count = 0
        self._file = None

    def __enter__(self):
        self._file = self.trace_file.open("w", encoding="utf-8")
        self._file.write("[\n")  # Start JSON array
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file:
            self._file.write("\n]")  # Close JSON array
            self._file.close()
            self._file = None

    def write_event(self, event) -> dict:
        """
        Write a single event to the trace file and return its dict representation.
        Returns the dict for immediate use (e.g., stats extraction).
        """
        event_dict = event_to_dict(event)

        if self._file:
            if self.event_count > 0:
                self._file.write(",\n")

            # Write with indent for readability, using default=str for non-serializable types
            json_str = json.dumps(event_dict, indent=2, default=str)
            # Indent each line for proper array formatting
            indented = "\n".join("  " + line for line in json_str.split("\n"))
            self._file.write(indented)
            self._file.flush()  # Ensure data is written to disk

            self.event_count += 1

        return event_dict


def extract_final_text_from_dicts(trace_dicts: list) -> str:
    """Extract final output text from trace event dicts."""
    for event in reversed(trace_dicts):
        if "content" in event and event["content"]:
            c = event["content"]
            if "parts" in c:
                for p in c["parts"]:
                    if p and "text" in p and p["text"]:
                        return p["text"]
    return "No final text found in trace."


def format_run_stats_md(stats: dict, run_duration_seconds: float) -> str:
    """Format run stats into a markdown section."""
    lines = [
        "## Run Statistics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Run Duration | {run_duration_seconds:.1f}s |",
        f"| Total Tool Calls | {stats['total_tool_calls']} |",
        f"| Search Agent Calls | {stats['search_agent_calls']} |",
        f"| Page Fetches | {stats['fetch_calls']} |",
        f"| YouTube Searches | {stats['youtube_search_calls']} |",
        f"| YouTube Viewer Calls | {stats['youtube_viewer_calls']} |",
        f"| URL Verifications | {stats['verify_urls_calls']} |",
        f"| Final Prompt Tokens | {stats['final_prompt_tokens']:,} |",
        f"| Final Total Tokens | {stats['final_total_tokens']:,} |",
        "",
    ]
    return "\n".join(lines)


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


def format_research_to_md(data: dict, timestamp: str, stats_md: str = "") -> str:
    """Format parsed research JSON into readable markdown."""
    lines = [
        "# AI News Research Results",
        "",
        f"**Generated:** {timestamp}",
        "",
    ]

    # Insert run stats right after the header if provided
    if stats_md:
        lines.append(stats_md)

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


def write_results_to_md(text: str, output_path: Path, timestamp: str, stats_md: str = "") -> None:
    """Write extracted text to markdown file, parsing JSON if possible."""
    parsed = extract_json_from_text(text)

    if parsed and "news" in parsed:
        formatted = format_research_to_md(parsed, timestamp, stats_md)
        output_path.write_text(formatted, encoding="utf-8")
    else:
        # Fallback: include stats even if JSON parsing fails
        header = f"# Research Agent Run\n\n**Generated:** {timestamp}\n\n"
        if stats_md:
            header += stats_md + "\n"
        content = header + text
        output_path.write_text(content, encoding="utf-8")


async def run_research_agent() -> tuple[Path, Path]:
    """
    Run the research agent and save results.

    Returns:
        Tuple of (md_file_path, trace_file_path)

    Raises:
        Exception: Re-raises any exception after cleaning up partial outputs.
    """
    import time

    start_time = time.time()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_readable = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # File paths for this run (tracked for cleanup on failure)
    trace_file = RESEARCH_HISTORY_DIR / f"trace_{timestamp}.json"
    md_file = RESEARCH_HISTORY_DIR / f"research_{timestamp}.md"

    # Clear token usage and agent notes at start (fresh run)
    clear_token_usage()
    clear_agent_notes()

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

    # Keep only recent event dicts for final text extraction and stats
    # (we only need the last few events for final text, and accumulate stats incrementally)
    recent_events: list[dict] = []
    max_recent_events = 20  # Keep last N events for final text extraction

    # Accumulate stats incrementally instead of re-processing all events
    stats = {
        "total_tool_calls": 0,
        "search_agent_calls": 0,
        "fetch_calls": 0,
        "youtube_search_calls": 0,
        "youtube_viewer_calls": 0,
        "x_search_calls": 0,
        "verify_urls_calls": 0,
        "final_prompt_tokens": 0,
        "final_total_tokens": 0,
    }

    try:
        # Stream events to disk incrementally to minimize memory usage
        with TraceWriter(trace_file) as trace_writer:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=user_message,
            ):
                # Write event to disk immediately and get dict representation
                event_dict = trace_writer.write_event(event)

                # Keep recent events for final text extraction (sliding window)
                recent_events.append(event_dict)
                if len(recent_events) > max_recent_events:
                    recent_events.pop(0)

                # Update stats incrementally from this event
                content = event_dict.get("content", {})
                parts = content.get("parts", []) if content else []
                for part in parts:
                    if part and part.get("function_call"):
                        func_name = part["function_call"].get("name", "")
                        stats["total_tool_calls"] += 1
                        if func_name == "google_search_agent":
                            stats["search_agent_calls"] += 1
                        elif func_name == "x_grok_research_agent":
                            stats["x_search_calls"] += 1
                        elif func_name == "fetch_page_content":
                            stats["fetch_calls"] += 1
                        elif func_name == "youtube_search_tool":
                            stats["youtube_search_calls"] += 1
                        elif func_name == "youtube_viewer_agent":
                            stats["youtube_viewer_calls"] += 1
                        elif func_name == "verify_urls":
                            stats["verify_urls_calls"] += 1

                # Track token usage (last event with usage wins)
                usage = event_dict.get("usage_metadata")
                if usage:
                    prompt_tokens = usage.get("prompt_token_count", 0)
                    total_tokens = usage.get("total_token_count", 0)
                    if prompt_tokens:
                        stats["final_prompt_tokens"] = prompt_tokens
                        update_token_usage(prompt_tokens, total_tokens)
                    if total_tokens:
                        stats["final_total_tokens"] = total_tokens

        # Calculate run duration
        run_duration = time.time() - start_time

        # Format stats for markdown
        stats_md = format_run_stats_md(stats, run_duration)

        # Extract final text from recent events (memory-efficient)
        final_text = extract_final_text_from_dicts(recent_events)
        write_results_to_md(final_text, md_file, timestamp_readable, stats_md)

        # Success: clean up previous run's files (keep only current run's results)
        cleanup_previous_run()

        # Clean up token usage file and agent notes after successful run
        clear_token_usage()
        clear_agent_notes()

        return md_file, trace_file

    except Exception:
        # Failed run: clean up THIS run's partial outputs, preserve previous run's results
        cleanup_failed_run(trace_file, md_file)
        raise
