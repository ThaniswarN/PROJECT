"""
test_pipeline_mocked.py

Verifies the full pipeline (schema extraction -> safety layers -> execution
-> self-correction) using REALISTIC mocked LLM outputs -- i.e. exactly the
kind of {sql, explanation, is_safe} JSON Claude would return per the system
prompt in app.py. This lets us prove out every non-LLM piece of the system
(which is where all the safety guarantees actually live) without needing
network access to the Anthropic API from this sandbox.

The 4th question ("Delete all customers") is tested TWICE:
  (a) assuming the model correctly refuses (is_safe: false) -- the expected
      real-world behavior per the system prompt, and
  (b) assuming, worst-case, the model misbehaves and returns a DELETE
      statement anyway -- to prove Layer 1/2 catch it independently of the
      model's own judgement.
"""

import textwrap
from unittest.mock import patch

import app


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


def run_case(question, mock_side_effect):
    schema = app.extract_schema()
    with patch("app.nl_to_sql", side_effect=mock_side_effect):
        result = app.answer_question(client=None, schema=schema, question=question)
    print_result(question, result)
    return result


def main():
    # ------------------------------------------------------------------
    # Q1: simple lookup -- realistic Claude output
    # ------------------------------------------------------------------
    q1 = "Show me all customers from New York"

    def q1_mock(client, schema, question, prior_error=None):
        return {
            "sql": "SELECT id, name, email, city, signup_date FROM customers WHERE city = 'New York';",
            "explanation": (
                "This looks through the list of customers and shows only the "
                "ones whose city is 'New York', including their name, email, "
                "and the date they signed up."
            ),
            "is_safe": True,
        }

    r1 = run_case(q1, q1_mock)
    assert r1["df"] is not None and len(r1["df"]) >= 3, "Expected at least 3 New York customers"
    assert not r1["blocked"]

    # ------------------------------------------------------------------
    # Q2: join -- realistic Claude output
    # ------------------------------------------------------------------
    q2 = "What products did customer with id 3 order?"

    def q2_mock(client, schema, question, prior_error=None):
        return {
            "sql": (
                "SELECT o.id AS order_id, p.name AS product_name, "
                "p.category, oi.quantity, o.order_date, o.status\n"
                "FROM orders o\n"
                "JOIN order_items oi ON oi.order_id = o.id\n"
                "JOIN products p ON p.id = oi.product_id\n"
                "WHERE o.customer_id = 3\n"
                "ORDER BY o.order_date;"
            ),
            "explanation": (
                "This finds every order placed by the customer whose ID is 3, "
                "then looks up which products were in each of those orders, "
                "showing the product name, category, how many were bought, "
                "the order date, and the order's status."
            ),
            "is_safe": True,
        }

    r2 = run_case(q2, q2_mock)
    assert r2["df"] is not None and len(r2["df"]) == 3, "Expected 3 order-item rows for customer 3"
    assert not r2["blocked"]

    # ------------------------------------------------------------------
    # Q3: aggregation -- realistic Claude output
    # ------------------------------------------------------------------
    q3 = "What are the top 5 products by total revenue?"

    def q3_mock(client, schema, question, prior_error=None):
        return {
            "sql": (
                "SELECT p.name AS product_name, p.category, "
                "SUM(oi.quantity * p.price) AS total_revenue\n"
                "FROM order_items oi\n"
                "JOIN products p ON p.id = oi.product_id\n"
                "GROUP BY p.id\n"
                "ORDER BY total_revenue DESC\n"
                "LIMIT 5;"
            ),
            "explanation": (
                "This adds up how much money each product has brought in "
                "(price multiplied by quantity sold across all orders), then "
                "shows the 5 products that earned the most revenue overall."
            ),
            "is_safe": True,
        }

    r3 = run_case(q3, q3_mock)
    assert r3["df"] is not None and len(r3["df"]) == 5, "Expected exactly 5 rows (top 5)"
    assert not r3["blocked"]
    # revenue should be sorted descending
    revs = r3["df"]["total_revenue"].tolist()
    assert revs == sorted(revs, reverse=True), "Expected revenue sorted descending"

    # ------------------------------------------------------------------
    # Q4a: adversarial -- model behaves correctly and refuses
    # ------------------------------------------------------------------
    q4 = "Delete all customers"

    def q4_mock_refuses(client, schema, question, prior_error=None):
        return {
            "sql": "",
            "explanation": (
                "I can't do that -- deleting data is a write/modify operation, "
                "and this tool is only allowed to look up information, never "
                "change or remove it. If you want, I can show you the list of "
                "all customers instead."
            ),
            "is_safe": False,
        }

    r4a = run_case(q4 + "  [model refuses]", q4_mock_refuses)
    assert r4a["blocked"] is True
    assert r4a["df"] is None

    # ------------------------------------------------------------------
    # Q4b: adversarial -- WORST CASE, model misbehaves and returns a
    # DELETE statement anyway. Layer 1 (and Layer 2 as backstop) must
    # still block it independently of the model's own judgement.
    # ------------------------------------------------------------------
    def q4_mock_misbehaves(client, schema, question, prior_error=None):
        return {
            "sql": "DELETE FROM customers;",
            "explanation": "This removes every row from the customers table.",
            "is_safe": True,  # pretend the model incorrectly said this was fine
        }

    r4b = run_case(q4 + "  [worst case: model returns DELETE anyway]", q4_mock_misbehaves)
    assert r4b["blocked"] is True, "Layer 1 must block DELETE even if model claims is_safe=True"
    assert r4b["df"] is None
    assert "DELETE" in r4b["blocked_reason"].upper() or "keyword" in r4b["blocked_reason"].lower()

    # Confirm the customers table is untouched (still 28 rows) after all tests
    import sqlite3
    conn = sqlite3.connect(app.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    conn.close()
    assert count == 28, f"customers table should still have 28 rows, has {count}"

    print("=" * 80)
    print("ALL ASSERTIONS PASSED ✅ -- customers table still has "
          f"{count} rows (untouched).")


if __name__ == "__main__":
    main()
