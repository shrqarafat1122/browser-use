
import os
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from browser_use import Agent, Browser, ChatOpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Browser Use API")


class TaskRequest(BaseModel):
    task: str


@app.get("/")
def home():
    return {
        "status": "running",
        "service": "Browser Use API"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
async def run_task(
    body: TaskRequest,
    x_api_key: str = Header(default="")
):
    secret = os.getenv("SERVICE_API_KEY", "")

    if not secret or not hmac.compare_digest(x_api_key, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not body.task.strip() or len(body.task) > 2000:
        raise HTTPException(
            status_code=400,
            detail="Task must contain 1–2000 characters"
        )

    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is missing"
        )

    try:
        agent = Agent(
            task=body.task,
            llm=ChatOpenAI(model="gpt-4.1-mini"),
            browser=Browser(use_cloud=True),
        )

        
history = await agent.run()

logger.info("Agent is_done: %s", history.is_done())
logger.info("Agent final_result: %r", history.final_result())

try:
    actions = history.action_names()
    logger.info("Agent actions: %r", actions)
except Exception:
    logger.exception("Could not read agent actions")

return {
    "status": "debug",
    "is_done": history.is_done(),
    "result": history.final_result(),
}
