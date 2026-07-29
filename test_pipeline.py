"""
test_pipeline.py
Exercises the core app.py pipeline (schema extraction -> NL->SQL -> 3-layer
safety -> execution -> self-correction) against the 4 required test
questions, without needing a running Streamlit server.
"""

import os
import sys
import textwrap

from app import extract_schema, answer_question, get_client

QUESTIONS = [
    "Show me all customers from New York",
    "What products did customer with id 3 order?",
    "What are the top 5 products by total revenue?",
    "Delete all customers",
]


def print_result(question: str, result: dict) -> None:
    print("=" * 80)
    print(f"QUESTION: {question}")
    print("-" * 80)
    print(f"Model self-check is_safe : {result['is_safe_llm']}")
    print(f"LLM-generated SQL        :\n{textwrap.indent(result['llm_sql'] or '(none)', '    ')}")
    print(f"Layer 1 pass             : {result['layer1_pass']}  ({result['layer1_msg']})")
    print(f"Layer 3 note             : {result['layer3_msg']}")
    print(f"Final SQL executed       :\n{textwrap.indent(result['final_sql'] or '(none)', '    ')}")
    print(f"Retried after error?     : {result['retried']}")
    print(f"Blocked?                 : {result['blocked']}  Reason: {result['blocked_reason']}")
    print(f"Execution error          : {result['exec_error']}")
    if result["df"] is not None:
        print(f"Rows returned            : {len(result['df'])}")
        print(result["df"].head(10).to_string(index=False))
    print(f"Explanation              : {result['explanation']}")
    print()


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set in the environment.")
        sys.exit(1)

    client = get_client()
    schema = extract_schema()

    for q in QUESTIONS:
        result = answer_question(client, schema, q)
        print_result(q, result)


if __name__ == "__main__":
    main()
