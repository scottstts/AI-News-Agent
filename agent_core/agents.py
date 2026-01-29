from pathlib import Path
import re
import json

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from pydantic import BaseModel, Field

from .fetch_tool import fetch_page_content
from .tools import get_date, get_previous_research_result, get_token_budget_info, read_notes, take_notes, verify_urls, youtube_search_tool


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")

def _get_google_search_urls(agent_output: str) -> list[str] | None:
    json_block_pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
    match = re.search(json_block_pattern, agent_output)
    if match:
        try:
            data = json.loads(match.group(1))
            urls_list = []
            for result in data["results"]:
                for url_entry in result["URLs"]:
                    urls_list.append(url_entry["URL"])

            return urls_list
        except (json.JSONDecodeError, KeyError):
            pass

    # Try to find raw JSON object (without code fence)
    json_pattern = r"\{[\s\S]*\"results\"[\s\S]*\}"
    match = re.search(json_pattern, agent_output)
    if match:
        try:
            data = json.loads(match.group(0))
            urls_list = []
            for result in data["results"]:
                for url_entry in result["URLs"]:
                    urls_list.append(url_entry["URL"])

            return urls_list
        except (json.JSONDecodeError, KeyError):
            pass

    return None

class SearchAgentInput(BaseModel):
    objectives: str = Field(
        description="The search objectives provided by the main agent to look up fresh information via Google Search."
    )

class YoutubeViewInput(BaseModel):
    video_urls: list[str] = Field(
        description="YouTube video URLs to analyze. Generally provide 1-3 YouTube video URLs."
    )
    objectives: str = Field(
        description="A destailed instruction on what to look for in the videos; This objective will need to be targeted and detailed enough so that the subagent can follow it and provide the findings exactly as what the main agent would need. The objectives string is provided by the main agent."
    )

class XGrokResearchAgentInput(BaseModel):
    objectives: str = Field(
        description="The search objectives provided by the main agent to look up fresh information on X/Twitter."
    )

# Use this instead of AgentTool to append token usage info at the end of every tool call
class AgentToolWithTokenMessage(AgentTool):
    async def run_async(self, *, args, tool_context):
        agent_output = await super().run_async(args=args, tool_context=tool_context)

        return {
            "subagent_result": agent_output,
            "token_usage_info": get_token_budget_info(),
        }

# append auto url validation info
class GoogleSearchAgentTool(AgentTool):
    async def run_async(self, *, args, tool_context):
        agent_output = await super().run_async(args=args, tool_context=tool_context)

        urls = _get_google_search_urls(agent_output=agent_output)
        url_validation = verify_urls(urls=urls) if urls else "None"

        return {
            "subagent_result": agent_output,
            "token_usage_info": get_token_budget_info(),
            "auto_url_validation_result": url_validation,
        }


google_search_only_agent = Agent(
    name="google_search_agent",
    model="gemini-3-flash-preview",
    description="Looks up fresh information via Google Search and returns a json of text results and URLs for further inspection.",
    instruction=_load_prompt("google_search_agent_instruction.md"),
    tools=[google_search],
    input_schema=SearchAgentInput,
)

youtube_viewer_agent = Agent(
    name="youtube_viewer_agent",
    model="gemini-3-flash-preview",
    description="A subagent to view and analyze YouTube videos, and answer questions asked in objectives. The urls of the YouTube videos and the inspection objectives will be provided by the main agent.",
    instruction=_load_prompt("yt_viewer_agent_instruction.md"),
    input_schema=YoutubeViewInput,
)

x_grok_research_agent = Agent(
    name="x_grok_research_agent",
    model=LiteLlm(
        model="openrouter/x-ai/grok-4.1-fast",
        extra_body={"reasoning": {"effort": "low"}}
    ),
    description="A subagent dedicated to find hot and trending AI developments on X/Twitter. It expects the research objectives from the main agent.",
    instruction=_load_prompt("x_grok_research_agent_instructions.md"),
    input_schema=XGrokResearchAgentInput,
)

research_agent = Agent(
    name="research_agent",
    model=LiteLlm(
        model="openai/gpt-5.2",
        reasoning_effort="medium",
    ),
    description="The main research agent that researches the specified content by organizing subagents and using various tools.",
    tools=[
        GoogleSearchAgentTool(agent=google_search_only_agent),
        AgentToolWithTokenMessage(agent=youtube_viewer_agent),
        AgentToolWithTokenMessage(agent=x_grok_research_agent),
        fetch_page_content,
        get_date,
        get_previous_research_result,
        get_token_budget_info,
        read_notes,
        take_notes,
        verify_urls,
        youtube_search_tool,
    ],
    instruction=_load_prompt("research_agent_prompt.md").replace("<Sub_Topic_List>", _load_prompt("sub_topic_list.md")),
)