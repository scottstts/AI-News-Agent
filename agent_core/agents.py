from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from pydantic import BaseModel, Field

from .tools import fetch_page_content, get_date, get_previous_research_result, get_token_budget_info, verify_urls, youtube_search_tool

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")

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

research_agent = Agent(
    name="research_agent",
    model=LiteLlm(
        model="openai/gpt-5.2",
        reasoning_effort="medium",
    ),
    description="The main research agent that researches the specified content by organizing subagents and using various tools.",
    tools=[
        AgentTool(agent=google_search_only_agent),
        AgentTool(agent=youtube_viewer_agent),
        fetch_page_content,
        get_date,
        get_previous_research_result,
        get_token_budget_info,
        verify_urls,
        youtube_search_tool,
    ],
    instruction=_load_prompt("research_agent_prompt.md").replace("<Sub_Topic_List>", _load_prompt("sub_topic_list.md")),
)