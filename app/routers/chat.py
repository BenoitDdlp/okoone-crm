from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.database import get_db
from app.services.claude_advisor import ClaudeAdvisor

router = APIRouter(tags=["chat"])

advisor = ClaudeAdvisor()


class ChatMessage(BaseModel):
    message: str
    conversation_id: str | None = None


@router.post("/api/v1/chat", response_class=HTMLResponse)
async def chat_endpoint(request: Request, payload: ChatMessage):
    """Process a chat message and return Claude's HTML-formatted response.

    Called by htmx from the eval page chat widget. Returns an HTML fragment
    that gets appended to the chat history.
    """
    async with get_db() as db:
        # TODO: persist conversation history by conversation_id for multi-turn
        response_text = await advisor.chat(db, payload.message)

    # Convert markdown-ish response to simple HTML
    import re

    html = response_text
    # Code blocks
    html = re.sub(r"```json\s*\n(.*?)\n```", r'<pre class="chat-code"><code>\1</code></pre>', html, flags=re.DOTALL)
    html = re.sub(r"```\s*\n(.*?)\n```", r"<pre><code>\1</code></pre>", html, flags=re.DOTALL)
    # Bold
    html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html)
    # Headers
    html = re.sub(r"^### (.+)$", r"<h4 style='margin:0.5rem 0 0.25rem;font-size:0.85rem;'>\1</h4>", html, flags=re.MULTILINE)
    # Line breaks
    html = html.replace("\n", "<br>")

    return f"""<div class="chat-msg chat-msg-assistant">
    <div class="chat-avatar">C</div>
    <div class="chat-bubble">{html}</div>
</div>"""


@router.post("/api/v1/chat/analyze", response_class=HTMLResponse)
async def full_analysis(request: Request):
    """Run a full Claude analysis cycle and return results as HTML."""
    async with get_db() as db:
        result = await advisor.analyze_and_propose(db)

    import json
    import re

    html = result["analysis"]
    html = re.sub(r"```json\s*\n(.*?)\n```", r'<pre class="chat-code"><code>\1</code></pre>', html, flags=re.DOTALL)
    html = re.sub(r"```\s*\n(.*?)\n```", r"<pre><code>\1</code></pre>", html, flags=re.DOTALL)
    html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"^### (.+)$", r"<h4 style='margin:0.5rem 0 0.25rem;font-size:0.85rem;'>\1</h4>", html, flags=re.MULTILINE)
    html = html.replace("\n", "<br>")

    actions = ""
    if result.get("proposed_weights"):
        weights_json = json.dumps(result["proposed_weights"])
        actions += f"""<button class="outline" style="font-size:0.8rem;margin-top:0.5rem;"
            hx-post="/api/v1/eval/apply-weights"
            hx-vals='{{"criteria_json": {weights_json}}}'
            hx-swap="none"
            hx-confirm="Appliquer les poids proposes par Claude ?">
            Appliquer les poids
        </button>"""

    if result.get("proposed_queries"):
        for q in result["proposed_queries"]:
            kw = q.get("keywords", "")
            loc = q.get("location", "")
            actions += f"""<button class="outline secondary" style="font-size:0.8rem;margin:0.25rem 0.25rem 0 0;"
                hx-post="/api/v1/searches/"
                hx-vals='{{"keywords": "{kw}", "location": "{loc}"}}'
                hx-swap="none">
                + Query: {kw}
            </button>"""

    return f"""<div class="chat-msg chat-msg-assistant">
    <div class="chat-avatar">C</div>
    <div class="chat-bubble">
        {html}
        {actions}
    </div>
</div>"""


@router.post("/api/v1/chat/suggest-email/{prospect_id}", response_class=HTMLResponse)
async def suggest_email(prospect_id: int):
    """Ask Claude to suggest a personalized email approach for a prospect."""
    async with get_db() as db:
        result = await advisor.suggest_email_approach(db, prospect_id)

    import re

    html = result
    html = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", html)
    html = html.replace("\n", "<br>")

    return f"""<div class="chat-msg chat-msg-assistant">
    <div class="chat-avatar">C</div>
    <div class="chat-bubble">{html}</div>
</div>"""
