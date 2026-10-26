
import os
import hmac
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from browser_use import Agent, Browser, ChatOpenAI

app = FastAPI(title="Browser Use API")

class TaskRequest(BaseModel):
    task: str

@app.get("/")
def home():
    return {"status": "running", "service": "Browser Use API"}

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

    if len(body.task) > 2000:
        raise HTTPException(status_code=400, detail="Task too long")

    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OpenAI API key missing")

    agent = Agent(
    task=body.task,
    llm=ChatOpenAI(model="gpt-4.1-mini"),
    browser=Browser(use_cloud=True),
  )
    result = await agent.run()
    return {"result": result.final_result()}
