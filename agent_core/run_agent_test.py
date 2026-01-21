import asyncio
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

# Suppress experimental feature warnings from ADK
warnings.filterwarnings("ignore", message=".*EXPERIMENTAL.*")

# Add parent directory to path for standalone execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

from agent_core.agents import research_agent
from google.adk.runners import InMemoryRunner

# Setup directories
TEST_RESULTS_DIR = Path(__file__).resolve().parent.parent / "test_results"
TEST_RESULTS_DIR.mkdir(exist_ok=True)

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
        # Try to get text from various possible structures
        if hasattr(event, "content"):
            content = event.content
            if hasattr(content, "parts"):
                for part in content.parts:
                    if hasattr(part, "text") and part.text:
                        return part.text
            elif hasattr(content, "text") and content.text:
                return content.text
        # Check model_dump if available
        if hasattr(event, "model_dump"):
            d = event.model_dump()
            if "content" in d and d["content"]:
                c = d["content"]
                if "parts" in c:
                    for p in c["parts"]:
                        if "text" in p and p["text"]:
                            return p["text"]
    return "No final text found in trace."

def write_text_to_md(text: str, output_md: Path) -> None:
    """Write extracted text to markdown file."""
    with output_md.open("w", encoding="utf-8") as f:
        f.write(f"# Research Agent Test Run\n\n{text}")

async def run_test():
    """Run the research agent and save results."""
    print("Starting research agent test run...")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Setup runner
    runner = InMemoryRunner(agent=research_agent)

    # Run the agent with a proper task prompt
    user_message = "Research the latest AI development news from the past 24 hours as instructed."
    trace = await runner.run_debug(user_message)

    # Save full trace (convert events to dicts)
    trace_file = TEST_RESULTS_DIR / f"trace_{timestamp}.json"
    trace_data = [event_to_dict(e) for e in trace]
    with trace_file.open("w", encoding="utf-8") as f:
        json.dump(trace_data, f, indent=2, default=str)
    print(f"Saved trace to: {trace_file}")

    # Extract and save final output
    final_text = extract_final_text(trace)
    output_md = TEST_RESULTS_DIR / f"output_{timestamp}.md"
    write_text_to_md(final_text, output_md)
    print(f"Saved final output to: {output_md}")

    print("Test run complete!")

if __name__ == "__main__":
    asyncio.run(run_test())
