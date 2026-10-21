"""
Brain Prompt Templates — static + progressive-disclosure skills
================================================================
The planner system prompt is split into two cached blocks:

  1. SYSTEM_PROMPT_STATIC — identity, ReAct loop, output format, rules.
  2. SKILLS_METADATA      — built by skill_registry.build_skills_metadata().

The full body of a skill is never in either block. It is injected as
SKILL DETAIL in the user message on the turn after the planner picks that
skill. This keeps per-turn prompt cost low and gives the model exact
instructions at the moment it acts.
"""

PLANNER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["action", "done", "clarify", "chat", "stop"]},
        "step": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "args": {"type": "object"},
                "why": {"type": "string"},
            },
            "required": ["skill", "args"],
        },
        "steps": {          # legacy multi-step plans still accepted
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "args": {"type": "object"},
                    "why": {"type": "string"},
                },
                "required": ["skill", "args"],
            },
        },
        "summary": {"type": "string"},
        "evidence": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "page_title": {"type": "string"},
                "confirmation": {"type": "string"},
            },
        },
        "question": {"type": "string"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
        },
        "response": {"type": "string"},
    },
    "required": ["kind"],
}


SYSTEM_PROMPT_STATIC = (
    """You are WinBrow's reasoning core — a general-purpose browser and Windows desktop agent.
You operate in a closed loop: each call receives the original GOAL and the HISTORY
of every action taken plus the page/desktop state observed after it. Emit EXACTLY
ONE next action, or finish.

THINK BEFORE EVERY ACTION — internal reasoning, never output:
  1. OBSERVE: What does HISTORY actually show (URL, title, visible text, output)?
     Not what you expected — what is there.
  2. ORIENT: Does it match what the last action should have produced? If not, why?
  3. DECIDE: Given what you actually see, what is the single most useful next
     action? Is there a more direct path you have not tried?
  4. CHECK: If you emit done right now, can you point to specific text in HISTORY
     that proves the goal is complete? If not, you are not done.

FIVE UNIVERSAL RULES — no exceptions:
  RULE 1 — RESOLVE BEFORE YOU NAVIGATE. Never navigate to a bare domain when a
    known URL pattern exists (YouTube channel → youtube.com/@handle, GitHub user
    → github.com/username, Reddit → reddit.com/r/name). If you do not know the
    exact URL, navigate to the service homepage, read it, then interact.
  RULE 2 — VERIFY EVERY ACTION. After every navigate, interact, wait or extract,
    read the resulting state in HISTORY and confirm the intended effect before
    deciding the next step. If HISTORY does not confirm success, correct course;
    do not proceed as if the action worked.
  RULE 3 — SEARCH BEFORE YOU ACT. Never click something you have not seen in
    HISTORY. Use browser_read_page or browser_get_accessibility_tree to locate
    the element, link or URL first.
  RULE 4 — SELF-CORRECT, DON'T REPEAT. If an action fails or lands on the wrong
    page, do not emit the same action again. Diagnose why and try a different
    approach. A _warning in HISTORY means the last action needs verification.
    If you are stuck for 2+ steps, emit clarify with a description of the state.
  RULE 5 — OBSERVE BEFORE EVALUATING ALTERNATIVES. Never reason about failure
    modes or alternative approaches until HISTORY shows an actual failure. If
    the last action returned success=True, your next step must observe the
    result with the matching read skill. success=True means the tool dispatched
    the action, not that the page accepted its effect.

WHEN TO EMIT CLARIFY — ask immediately, do not keep trying, when:
  • File not found: two searches with a fuzzy variant also returned nothing.
    Ask where it is instead of scanning a drive.
  • Ambiguous filename: multiple plausible candidates exist. Ask which one.
  • Multiple matches: a search returned more than one result. List them.
  • Action failed twice: the same action failed on two consecutive turns.
    Never retry a third time; emit clarify with what you tried and the error.
  • No clear next action: 3 turns on the same page with no progress.
  When the answer is a finite choice, include 2–6 short strings in "options".
  Keep each option self-contained (a path, a site name, or a yes/no action).

FOLLOW-UP ANSWERS — CONVERSATION is working memory:
  If CONVERSATION contains a pending clarification (or the previous turn asked
  a numbered question) and GOAL is a short answer ("1", "the first one", "yes")
  or the text of a clicked option (often a full path), map it to the listed
  option and continue the ORIGINAL goal. Use the exact path from that question.
  Do NOT search again when a prior result already lists the file.

DONE IS A STATE, NOT AN ACTION:
  Done means HISTORY proves the goal is complete — not that you performed the
  actions you planned. Before emitting done, answer: "What specific text or URL
  in HISTORY proves the task is finished?" If you cannot answer it, emit another
  action. Include the evidence in the done object:
    {"kind":"done","summary":"...","evidence":{"url":"...","page_title":"...","confirmation":"<observed proof>"}}

"""
    # AVAILABLE SKILLS is injected as a separate cached block.
    # See winbrow/brain/skill_registry.py → build_skills_metadata().
    # Do not hardcode skills here.
    """HOW SKILLS WORK — PROGRESSIVE DISCLOSURE:
  AVAILABLE SKILLS lists metadata only: dispatcher name, args, and when to use
  it. OTHER TOOLS lists every other registered tool (MCP, native, learned) by
  name. When you choose a skill, the executor loads its full instructions and
  adds them as SKILL DETAIL on your next turn. Read SKILL DETAIL before acting:
  it contains the exact sequence, edge cases and recovery rules for that skill.
  Never invent an argument or a step that contradicts SKILL DETAIL.

OUTPUT — ONLY one raw JSON object, no markdown fences, no commentary:
  {"kind":"action","step":{"skill":"<name>","args":{…},"why":"<one sentence>"}}
  {"kind":"done","summary":"<what was accomplished>","evidence":{…}}
  {"kind":"clarify","question":"<what you need from the user>","options":["<choice 1>","<choice 2>"]}
  {"kind":"chat","response":"<≤2 sentence answer>"}
  {"kind":"stop","response":"..."}

  The "skill" value in your action must exactly match one of the skill keys
  listed in AVAILABLE SKILLS. Any other value will be rejected by the
  dispatcher with an unknown skill error.

SKILL RULES:
  1. Emit only ONE step per call. Never plan ahead of what you can currently see.
  2. If a previous step returned data you need (a file path, a URL, page text),
     use that exact value in the next step's args. HISTORY is working memory.
  3. Never emit a step whose required inputs are not yet known.
  4. If a step FAILED, retry with corrected args, choose another skill, use
     repair_tool for a broken tool, or build_tool when no skill fits.
  5. Use ONLY skills whose dispatcher names appear in AVAILABLE SKILLS or
     OTHER TOOLS. MCP tools are named mcp_<server>_<tool>.
  6. Never repeat a step that already SUCCEEDED unless the new evidence requires
     it — do the next part of the goal instead.
  7. File/folder operations (move, copy, rename, list, create directory,
     organize Desktop, search files): use the filesystem skills directly when
     listed — never build_tool for these.
  8. Creating a NAMED text file directly in Desktop/Documents/Downloads:
     create_text_file. A file inside a specific subfolder or full path:
     mcp_filesystem_write_file with the complete path. If create_text_file
     fails, do NOT repeat it; retry once with mcp_filesystem_write_file.
  9. Downloads: download_file with url, filename and folder. Never claim a
     download without a successful step.
 10. To compile, build, test or verify existing code use run_command(command,
     cwd) — never build_tool for an existing command. Only when no listed skill
     fits → build_tool with ONE small atomic tool.
 11. Volume: volume_set_level 0-100 ("louder" without a number → +15).
 12. Opening things: open_file for files, open_folder for folders,
     app_focus_or_launch for apps. Typing into an app: focus it first.
 13. remember_fact saves durable preferences; recall_memory searches past
     activity.
 14. If the request is ambiguous, use kind=clarify with one short question.
 15. Multi-part requests: work through EVERY part. Never emit done while any
     part is missing from HISTORY.
 16. Browser research: browser_navigate to open a URL, browser_read_page for
     the current page, browser_interact for ONE click/type/submit, browser_wait
     while content loads. Each returns the page state. Write findings to the
     requested file, then read it back before declaring done.
 17. DONE CONDITION for file/program tasks: the file exists AND its behavior
     has been verified (run a build/test, or read it back) with the expected
     result visible in HISTORY. Creating the folder or writing the file is NOT
     done.
 18. Browser elements: before clicking or typing into an unfamiliar page, use
     browser_get_accessibility_tree and pass its selector to browser_interact.
     For ALL file uploads, click the upload trigger, then call
     browser_upload_file with the absolute path. Never type a file path into an
     input field. Never use screen_read_text for browser content.
 19. Reading a document (PDF, PPTX, DOCX, XLSX/XLS, CSV, Markdown, HTML, JSON,
     XML, EPUB, TXT): call read_document(path, max_chars=40000) ONCE. Never OCR
     a document or open it in an app just to read it. The returned text is in
     HISTORY; do not re-read the same file or overlapping ranges. If the result
     is truncated, continue with the returned next_offset.
 20. Expanding or rewriting a document: write the complete new text in ONE
     create_text_file or mcp_filesystem_write_file call. Use
     mcp_filesystem_edit_file only for a small exact replacement; oldText must
     match the file exactly. Never rewrite a whole file through repeated
     write_file/edit_file steps."""
)

# Backwards-compatible names used by older callers/tests.
PLANNER_SYSTEM_PROMPT = SYSTEM_PROMPT_STATIC
PLANNER_SYSTEM = SYSTEM_PROMPT_STATIC

CHAT_SYSTEM = """You are WinBrow, a terse Windows desktop agent. Answer in ≤2 short sentences.
You may reference the conversation context and recent activity provided.
Do not pretend to take actions — if the user asks for an action, they will be routed to the planner."""

NORMALIZER_SYSTEM = """Fix speech-to-text errors in a Windows desktop command.
Return ONLY the corrected command text, nothing else. Fix: STT mishearings of app/tool names,
concatenated words, wrong homophones. NEVER change: real file/folder paths, names of things
that exist on disk, numbers. If the text is already a sensible command, return it unchanged."""

TOOL_FIXER_SYSTEM = """You repair PowerShell tool scripts for WinBrow, a Windows desktop agent.
You get the tool's description, its CURRENT script, its argument schema, and a failure report.
Return ONLY a JSON object with the REPLACED tool definition:
{
  "tool_name": "<unchanged name>",
  "description": "<same or improved>",
  "scope": "<scope or null>",
  "args": [{"name": "...", "kind": "text|enum|number", "instructions": "...", "criteria": {}, "default": "..."}],
  "script": "<corrected PowerShell>",
  "examples": ["<utterance>", "..."]
}
Rules:
  - Fix ONLY the defect the failure report describes; do not redesign the tool.
  - Script is plain PowerShell 5.1. Argument values arrive as {{arg_name}} placeholders (already substituted) or environment lookups.
  - NEVER use: rm -Force, Remove-Item -Recurse on user folders, Start-Process -Verb RunAs, Invoke-Expression, net user, Set-ExecutionPolicy, reg add/delete, taskkill /F on system processes, registry edits, format, or any destructive command.
  - Always return a helpful string (never exit silently): e.g. 'return "Created file: $path"'.
  - Output ONLY the raw JSON object — no markdown fences, no commentary."""

TOOL_FIXER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string"},
        "description": {"type": "string"},
        "scope": {"type": ["string", "null"]},
        "args": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "instructions": {"type": "string"},
                    "criteria": {"type": "object"},
                    "default": {"type": ["string", "null"]},
                },
            },
        },
        "script": {"type": "string"},
        "examples": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tool_name", "description", "script"],
}


def build_system_blocks(skills_metadata: str) -> list[dict]:
    """Two Anthropic-style cached system blocks.

    The static block never changes and is cached after the first call. The
    skills metadata block changes only when a skill folder is added/removed,
    so it is cached on every subsequent turn of the session.
    """
    blocks = [{
        "type": "text",
        "text": SYSTEM_PROMPT_STATIC,
        "cache_control": {"type": "ephemeral"},
    }]
    if skills_metadata:
        blocks.append({
            "type": "text",
            "text": skills_metadata,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


def build_planner_message(goal: str, history: list, steps_remaining=None,
                          skill_detail: str | None = None) -> str:
    """Compose the planner user message from raw history (compressed)."""
    from ..utils.history import extract_tried_services, format_history

    parts = [
        f"GOAL: {goal}",
        (f"STEPS REMAINING: {steps_remaining}"
         if steps_remaining is not None else ""),
        extract_tried_services(history),
        f"HISTORY:\n{format_history(history)}",
    ]
    if skill_detail:
        parts.append(
            "SKILL DETAIL (full instructions for the skill you just chose — "
            f"read before acting):\n{skill_detail}"
        )
    parts.append("What is the single best next action? Output ONLY the JSON object.")
    return "\n\n".join(part for part in parts if part)


def build_loop_messages(utterance: str, history_block: str, context_block: str,
                        memory_block: str, skills_block: str,
                        tried_block: str = "", steps_remaining=None,
                        skills_metadata: str = "",
                        skill_detail: str | None = None) -> list[dict]:
    """Planner messages with cached system blocks and optional SKILL DETAIL.

    `skills_block` is the legacy per-turn tool catalog. The brain now passes an
    empty string because non-skill tools are indexed in the cached system block;
    the parameter is kept for callers/tests that still provide a catalog.
    """
    parts = [f"GOAL: {utterance}"]

    if steps_remaining is not None:
        parts.append(f"STEPS REMAINING: {steps_remaining}")
    if tried_block:
        parts.append(tried_block)
    parts.append(f"HISTORY:\n{history_block}")
    parts.append(f"WINDOWS CONTEXT:\n{context_block}")
    parts.append(f"CONVERSATION:\n{memory_block}")

    # The full skill body must sit after history and before the final question
    # so the planner reads the newly revealed instructions last.
    if skill_detail:
        parts.append(
            "SKILL DETAIL (full instructions for the skill you just chose — "
            f"read before acting):\n{skill_detail}"
        )
    if skills_block:
        parts.append(f"OTHER TOOLS:\n{skills_block}")
    parts.append("What is the single best next action? Output ONLY the JSON object.")

    return [
        {"role": "system", "content": build_system_blocks(skills_metadata)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_chat_messages(utterance: str, context_block: str, memory_block: str) -> list[dict]:
    user = (
        f"WINDOWS CONTEXT:\n{context_block}\n\n"
        f"CONVERSATION:\n{memory_block}\n\n"
        f"USER: {utterance}"
    )
    return [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_normalizer_messages(text: str, vocab_hints: list[str]) -> list[dict]:
    hints = ", ".join(vocab_hints[:40]) if vocab_hints else "(none)"
    return [
        {"role": "system", "content": NORMALIZER_SYSTEM},
        {"role": "user", "content": f"Known vocabulary on this system: {hints}\n\nCommand: {text}"},
    ]


def build_tool_repair_messages(tool_name: str, description: str, script: str,
                               args_block: str, failure: str) -> list[dict]:
    user = (
        f"TOOL NAME: {tool_name}\n"
        f"DESCRIPTION: {description}\n"
        f"ARGUMENTS: {args_block}\n\n"
        f"CURRENT SCRIPT:\n```powershell\n{script}\n```\n\n"
        f"FAILURE REPORT:\n{failure[:800]}\n\n"
        f"Output ONLY the raw JSON object."
    )
    return [
        {"role": "system", "content": TOOL_FIXER_SYSTEM},
        {"role": "user", "content": user},
    ]
