import uuid
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import root_agent

APP = "coldwatch"
sessions = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP, session_service=sessions)
app = FastAPI()


class Ask(BaseModel):
    text: str
    session_id: str | None = None


@app.post("/api/ask")
async def ask(body: Ask):
    sid = body.session_id or str(uuid.uuid4())
    if not await sessions.get_session(app_name=APP, user_id="u", session_id=sid):
        await sessions.create_session(app_name=APP, user_id="u", session_id=sid)
    steps, answer = [], ""
    msg = types.Content(role="user", parts=[types.Part(text=body.text)])
    async for ev in runner.run_async(user_id="u", session_id=sid, new_message=msg):
        for fc in ev.get_function_calls():  # 可観測性: 実行ステップを返す
            steps.append({"tool": fc.name, "args": dict(fc.args or {})})
        if ev.is_final_response() and ev.content and ev.content.parts:
            answer = "".join(p.text or "" for p in ev.content.parts)
    return {"session_id": sid, "steps": steps, "answer": answer}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
