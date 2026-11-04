import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from agents import Agent, Runner

env_path = Path(__file__).parent.parent / "environment_vars.env"
load_dotenv(env_path)

print("API key loaded:", bool(os.getenv("OPENAI_API_KEY")))

agent = Agent(
    name="TA Ranker",
    instructions="You must take all the provided data on potential candidates and rank them.",
)

async def main():
    result = await Runner.run(
        agent,
        "TA 1: 3 years of experience, TA 2: 1 year of experience, TA 3: 2 years of experience"
    )
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())