# CogniCore Integrations

CogniCore provides drop-in memory and reflection capabilities for the most popular agent frameworks.

## Historical conversation example

[chatgpt-history.json](chatgpt-history.json) is a synthetic export with three related DIPY conversations, including an old decision and a tool attachment reference. [Import the example and inspect its sources over local MCP](../docs/history.md#import-and-recall-an-old-conversation).

## LangChain Integration

CogniCore integrates seamlessly with LangChain via the `CogniCoreCallbackHandler` (which automatically records your agent's LLM interactions) and a suite of `BaseTool` objects.

```python
import os
from langchain.chat_models import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from cognicore.integrations.langchain import cognicore_tools, CogniCoreCallbackHandler

# 1. Setup the LLM
llm = ChatOpenAI(model="gpt-4", temperature=0)

# 2. Add the Auto-Memory Handler
memory_tracker = CogniCoreCallbackHandler(category_prefix="my_app")
llm.callbacks = [memory_tracker]

# 3. Initialize Agent with CogniCore Tools (Recall, Reflect, ThreatScan)
agent = initialize_agent(
    tools=cognicore_tools(),
    llm=llm,
    agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION
)

agent.run("Analyze our past failures and recommend a new strategy.")
```

## CrewAI Integration

Empower your CrewAI analysts with persistent memory so they never make the same mistake twice across different crew executions.

```python
import os
from crewai import Agent, Task, Crew
from cognicore.integrations.crewai import cognicore_crewai_tools

# 1. Load the shared CogniCore Tools
memory_tools = cognicore_crewai_tools()

# 2. Assign to your Agent
analyst = Agent(
    role='Security Analyst',
    goal='Detect threats and recall past mitigations.',
    backstory="An expert analyst powered by persistent memory.",
    tools=memory_tools,
    llm="gpt-4"
)

# 3. Create a memory-dependent task
task = Task(
    description="Search your memory for 'authentication bypass' and summarize how we fixed it last time.",
    expected_output="A summary of the recalled mitigation.",
    agent=analyst
)

Crew(agents=[analyst], tasks=[task]).kickoff()
```

## Demos

| Folder | What it is |
|---|---|
| [`walkthrough/`](walkthrough/) | Numbered walkthrough: no memory, memory, persistence, semantic search, reflection, immune system, passports, LongMemEval |
| [`experience_transfer/`](experience_transfer/) | Structured experience demos: verification, failure transfer, supersession, integrations |
| [`standalone/`](standalone/) | Self-contained demos (Figma, GitHub, ElevenLabs pipelines, MCP launch) |
| [`launch/`](launch/) | Scripts used to record launch videos and GIFs |

Run from the repository root, e.g. `python examples/walkthrough/01_no_memory.py`.
