"""
NL → SQL Query Builder for SQLite
==================================
A Streamlit app that lets non-technical users ask plain-English questions
and get back safe, read-only SQL query results, powered by Claude.

Run with:  streamlit run app.py
"""

import json
import os
import re
import sqlite3

import pandas as pd
import streamlit as st
from google import genai

DB_PATH = "demo.db"
MODEL_NAME = "gemini-2.5-flash"  # free-tier Gemini model
DEFAULT_LIMIT = 1000
import sys
import subprocess

def ensure_database():
    """Rebuild demo.db if it's missing or corrupted (e.g. from a bad git push)."""
    needs_rebuild = False
    if not os.path.exists(DB_PATH):
        needs_rebuild = True
    else:
        try:
            test_conn = sqlite3.connect(DB_PATH)
            test_conn.execute("SELECT 1 FROM customers LIMIT 1")
            test_conn.close()
        except Exception:
            needs_rebuild = True

    if needs_rebuild:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)

        result = subprocess.run(
            [sys.executable, "build_db.py"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            st.error(
                "Failed to rebuild the database automatically.\n\n"
                f"stdout: {result.stdout}\n\nstderr: {result.stderr}"
            )
            st.stop()
# ---------------------------------------------------------------------------
# 1. Gemini client
# ---------------------------------------------------------------------------

def get_client() -> genai.Client:
    """Create a Gemini client using GEMINI_API_KEY from the environment."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        st.error(
            "No GEMINI_API_KEY found in the environment. "
            "Set it before running the app, e.g. `export GEMINI_API_KEY=AIza...`"
        )
        st.stop()
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# 2. SCHEMA EXTRACTION
# ---------------------------------------------------------------------------

def extract_schema(db_path: str = DB_PATH) -> str:
    """
    Connects to the SQLite file and pulls every CREATE TABLE statement
    (including foreign keys) straight from sqlite_master. This text is
    handed to the LLM as schema context.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        statements = [row[0] for row in cur.fetchall()]
        return "\n\n".join(statements)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. NL → SQL AGENT (core logic)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a careful SQL assistant for a SQLite database. \
You translate a non-technical user's plain-English question into a SINGLE, \
read-only SQL SELECT statement that answers it, using ONLY the schema \
provided to you.

STRICT RULES:
- You may ONLY generate a single SELECT statement (optionally wrapped in a \
  read-only CTE using WITH ... SELECT). NEVER generate INSERT, UPDATE, \
  DELETE, DROP, ALTER, ATTACH, PRAGMA, CREATE, REPLACE, or any statement \
  that writes to or modifies the database or its schema.
- NEVER generate more than one statement. Do not use semicolons to chain \
  multiple statements together.
- Only reference tables and columns that actually exist in the provided \
  schema. Do not invent column or table names.
- If the question cannot be answered with a read-only SELECT against this \
  schema (e.g. it asks to change data, or it's not answerable from the \
  schema), set "is_safe" to false, leave "sql" as an empty string, and use \
  "explanation" to say why in plain English.
- Always write a short, plain-English explanation of what the SQL does, \
  suitable for someone who has never written SQL, avoiding jargon.
- Respond with ONLY a single JSON object, no other text, no markdown code \
  fences. The JSON object must have exactly these keys:
  {
    "sql": "<the SELECT statement, or empty string if not safe/possible>",
    "explanation": "<plain-English explanation for a non-technical user>",
    "is_safe": true or false
  }
"""


def _build_user_prompt(schema: str, question: str, prior_error: str | None = None) -> str:
    prompt = f"Database schema:\n{schema}\n\nUser question: {question}\n"
    if prior_error:
        prompt += (
            "\nYour previous SQL attempt failed when executed against the "
            f"real database with this error:\n{prior_error}\n"
            "Please fix the SQL and try again, keeping it a single read-only "
            "SELECT statement."
        )
    return prompt


def _parse_llm_json(raw_text: str) -> dict:
    """Best-effort extraction of a JSON object from the model's reply."""
    text = raw_text.strip()
    # Strip markdown code fences if the model added them anyway.
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # fall back: grab the first {...} blob
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def nl_to_sql(
    client: genai.Client,
    schema: str,
    question: str,
    prior_error: str | None = None,
) -> dict:
    """
    Calls Gemini with the schema + question and returns a dict:
        { "sql": str, "explanation": str, "is_safe": bool }
    """
    user_prompt = _build_user_prompt(schema, question, prior_error)

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=user_prompt,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "max_output_tokens": 1000,
        },
    )

    raw_text = response.text or ""

    try:
        parsed = _parse_llm_json(raw_text)
    except Exception:
        parsed = {
            "sql": "",
            "explanation": (
                "The assistant did not return a usable response. "
                "Please try rephrasing your question."
            ),
            "is_safe": False,
        }

    # Make sure all expected keys exist with sane defaults.
    parsed.setdefault("sql", "")
    parsed.setdefault("explanation", "")
    parsed.setdefault("is_safe", False)
    return parsed


# ---------------------------------------------------------------------------
# 4. SAFETY LAYER (3 layers)
# ---------------------------------------------------------------------------

# Layer 1: keyword / structure validation -----------------------------------

FORBIDDEN_KEYWORDS = [
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER",
    "ATTACH", "DETACH", "PRAGMA", "CREATE", "REPLACE",
    "VACUUM", "REINDEX", "TRIGGER",
]


def layer1_validate(sql: str) -> tuple[bool, str]:
    """
    Regex/keyword validation. Rejects:
      - anything that isn't a single SELECT (or WITH ... SELECT) statement
      - multiple semicolon-separated statements
      - any forbidden write/DDL keyword, anywhere in the query
    Returns (is_valid, message).
    """
    if not sql or not sql.strip():
        return False, "No SQL was generated."

    stripped = sql.strip()

    # Reject multiple statements: allow at most one trailing semicolon.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        return False, "Blocked: multiple semicolon-separated statements are not allowed."

    # Must start with SELECT or a WITH ... SELECT common table expression.
    first_word_match = re.match(r"^\s*(\w+)", stripped, re.IGNORECASE)
    first_word = first_word_match.group(1).upper() if first_word_match else ""
    if first_word not in ("SELECT", "WITH"):
        return False, f"Blocked: only SELECT statements are allowed (got '{first_word}')."

    # Keyword blacklist — check as whole words, case-insensitive.
    upper_sql = stripped.upper()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper_sql):
            return False, f"Blocked: forbidden keyword '{kw}' detected in the query."

    return True, "Passed keyword/structure validation."


# Layer 2: read-only connection ---------------------------------------------

def get_readonly_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Opens the SQLite file in read-only URI mode. This is a hard backstop:
    even if a malicious/mistaken write statement slipped past Layer 1,
    SQLite itself will refuse to execute it against a read-only file handle.
    """
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# Layer 3: auto-append LIMIT -------------------------------------------------

def layer3_enforce_limit(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """
    Auto-appends a LIMIT clause to any query that doesn't already have one,
    so a broad question can never pull back an unbounded result set.
    """
    stripped = sql.strip().rstrip(";").strip()
    if re.search(r"\bLIMIT\s+\d+\b", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped}\nLIMIT {limit}"


def run_safety_pipeline(sql: str) -> dict:
    """
    Runs all 3 safety layers in order. Returns a dict describing the outcome
    of each layer plus the (possibly modified) final SQL, so the UI can
    show a transparent safety report.
    """
    report = {
        "layer1_pass": False,
        "layer1_msg": "",
        "layer3_msg": "",
        "final_sql": sql,
    }

    ok, msg = layer1_validate(sql)
    report["layer1_pass"] = ok
    report["layer1_msg"] = msg
    if not ok:
        return report

    limited_sql = layer3_enforce_limit(sql)
    if limited_sql != sql.strip().rstrip(";").strip():
        report["layer3_msg"] = "Query had no LIMIT clause — automatically capped at " \
                                f"{DEFAULT_LIMIT} rows."
    else:
        report["layer3_msg"] = "Query already had a LIMIT clause — left unchanged."
    report["final_sql"] = limited_sql

    return report


def execute_readonly(sql: str, db_path: str = DB_PATH) -> tuple[pd.DataFrame | None, str | None]:
    """
    Layer 2 in action: executes the query against a read-only connection.
    Returns (dataframe, None) on success or (None, error_message) on failure.
    """
    try:
        conn = get_readonly_connection(db_path)
        try:
            df = pd.read_sql_query(sql, conn)
            return df, None
        finally:
            conn.close()
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# 5. Orchestration: NL question -> validated, executed result (with 1 retry)
# ---------------------------------------------------------------------------

def answer_question(client: genai.Client, schema: str, question: str) -> dict:
    """
    Full pipeline for a single user question:
      1. Ask Claude for SQL + explanation.
      2. Run it through the 3-layer safety pipeline.
      3. Execute it read-only.
      4. If execution errors, retry once by feeding the error back to Claude.
    Returns a dict with everything the UI needs to render.
    """
    result = {
        "question": question,
        "llm_sql": "",
        "explanation": "",
        "is_safe_llm": False,
        "layer1_pass": False,
        "layer1_msg": "",
        "layer3_msg": "",
        "final_sql": "",
        "df": None,
        "exec_error": None,
        "retried": False,
        "blocked": False,
        "blocked_reason": "",
    }

    llm_out = nl_to_sql(client, schema, question)
    result["llm_sql"] = llm_out.get("sql", "")
    result["explanation"] = llm_out.get("explanation", "")
    result["is_safe_llm"] = bool(llm_out.get("is_safe", False))

    if not result["is_safe_llm"] or not result["llm_sql"]:
        result["blocked"] = True
        result["blocked_reason"] = result["explanation"] or (
            "The assistant determined this question cannot be answered "
            "with a safe, read-only query."
        )
        return result

    # --- Safety pipeline (layers 1 & 3; layer 2 is enforced at execution) ---
    safety = run_safety_pipeline(result["llm_sql"])
    result["layer1_pass"] = safety["layer1_pass"]
    result["layer1_msg"] = safety["layer1_msg"]
    result["layer3_msg"] = safety["layer3_msg"]
    result["final_sql"] = safety["final_sql"]

    if not safety["layer1_pass"]:
        result["blocked"] = True
        result["blocked_reason"] = safety["layer1_msg"]
        return result

    # --- Execute (layer 2: read-only connection) ---
    df, err = execute_readonly(result["final_sql"])
    if err is None:
        result["df"] = df
        return result

    # --- Self-correction: retry once with the error fed back to the LLM ---
    result["exec_error"] = err
    retry_out = nl_to_sql(client, schema, question, prior_error=err)
    result["retried"] = True

    retry_sql = retry_out.get("sql", "")
    retry_safe = bool(retry_out.get("is_safe", False))
    retry_explanation = retry_out.get("explanation", "")

    if not retry_safe or not retry_sql:
        result["blocked"] = True
        result["blocked_reason"] = retry_explanation or (
            "The assistant could not produce a safe corrected query."
        )
        return result

    safety2 = run_safety_pipeline(retry_sql)
    if not safety2["layer1_pass"]:
        result["blocked"] = True
        result["blocked_reason"] = safety2["layer1_msg"]
        return result

    df2, err2 = execute_readonly(safety2["final_sql"])

    # Update result with the retried attempt's details for display.
    result["llm_sql"] = retry_sql
    result["explanation"] = retry_explanation
    result["layer1_pass"] = safety2["layer1_pass"]
    result["layer1_msg"] = safety2["layer1_msg"]
    result["layer3_msg"] = safety2["layer3_msg"]
    result["final_sql"] = safety2["final_sql"]

    if err2 is None:
        result["df"] = df2
        result["exec_error"] = None
    else:
        result["exec_error"] = err2

    return result


# ---------------------------------------------------------------------------
# 6. STREAMLIT UI
# ---------------------------------------------------------------------------

def render_ui():
    ensure_database()

    st.set_page_config(
        page_title="NL → SQL Query Builder",
        page_icon="🗃️",
        layout="centered",
    )

    st.markdown(
        """
        <style>
        .safety-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: 600;
            margin: 2px 6px 2px 0;
        }
        .badge-pass { background-color: #DCFCE7; color: #166534; }
        .badge-fail { background-color: #FEE2E2; color: #991B1B; }
        .badge-info { background-color: #DBEAFE; color: #1E40AF; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("🗃️ NL → SQL Query Builder")
    st.caption("Ask a question in plain English about the demo e-commerce database. "
               "Only safe, read-only SELECT queries are ever executed.")

    if not os.path.exists(DB_PATH):
        st.error(f"Database file '{DB_PATH}' not found. Run `python build_db.py` first.")
        st.stop()

    with st.expander("📋 View database schema"):
        schema_text = extract_schema()
        st.code(schema_text, language="sql")

    question = st.text_input(
        "Your question",
        placeholder="e.g. Show me all customers from New York",
    )
    run_clicked = st.button("Run Query", type="primary")

    if run_clicked:
        if not question or not question.strip():
            st.warning("Please type a question before running a query.")
            return

        client = get_client()
        schema = extract_schema()

        with st.spinner("Thinking through your question..."):
            result = answer_question(client, schema, question.strip())

        st.divider()

        # --- a) Generated SQL ---
        st.subheader("Generated SQL")
        sql_to_show = result["final_sql"] or result["llm_sql"]
        if sql_to_show:
            st.code(sql_to_show, language="sql")
        else:
            st.code("-- no SQL generated --", language="sql")

        # --- b) Safety status badges ---
        st.subheader("Safety status")
        badges_html = ""
        if result["is_safe_llm"]:
            badges_html += '<span class="safety-badge badge-pass">✅ Model self-check: safe</span>'
        else:
            badges_html += '<span class="safety-badge badge-fail">🚫 Model self-check: unsafe</span>'

        if result["llm_sql"]:
            if result["layer1_pass"]:
                badges_html += '<span class="safety-badge badge-pass">✅ Layer 1: keyword/structure check passed</span>'
            else:
                badges_html += '<span class="safety-badge badge-fail">🚫 Layer 1: keyword/structure check FAILED</span>'

        if result["final_sql"] and result["df"] is not None:
            badges_html += '<span class="safety-badge badge-pass">✅ Layer 2: executed via read-only connection</span>'
        elif result["final_sql"]:
            badges_html += '<span class="safety-badge badge-info">ℹ️ Layer 2: read-only connection enforced (execution failed)</span>'

        if result.get("layer3_msg"):
            badges_html += '<span class="safety-badge badge-info">🧮 Layer 3: LIMIT enforced</span>'

        if result["retried"]:
            badges_html += '<span class="safety-badge badge-info">🔁 Self-correction: retried once after an error</span>'

        st.markdown(badges_html, unsafe_allow_html=True)

        if result["layer3_msg"]:
            st.caption(result["layer3_msg"])

        # --- Blocked case ---
        if result["blocked"]:
            st.error(
                "⚠️ This request was blocked before it could touch the database.\n\n"
                f"**Reason:** {result['blocked_reason']}"
            )
            return

        # --- Execution error (after retry) ---
        if result["exec_error"]:
            st.error(
                "The query still failed after one automatic correction attempt.\n\n"
                f"**Database error:** {result['exec_error']}"
            )
            return

        # --- c) Results table ---
        st.subheader("Results")
        df = result["df"]
        if df is not None and not df.empty:
            st.dataframe(df, use_container_width=True)
            st.caption(f"{len(df)} row(s) returned.")
        elif df is not None:
            st.info("The query ran successfully but returned no rows.")

        # --- d) Plain-English explanation ---
        st.subheader("Explanation")
        st.write(result["explanation"] or "_No explanation was provided._")


if __name__ == "__main__":
    render_ui()
