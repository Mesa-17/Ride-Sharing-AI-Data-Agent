"""Ask the data agent a question from the command line.

    python main.py "Which 5 cities had the most completed rides?"
    python main.py --show-sql "Average fare by payment method"
    python main.py                      # interactive mode
"""
import argparse
import sys

from agents.data_agent import build_data_agent


def ask(agent, question: str, show_sql: bool) -> None:
    out = agent.invoke({"question": question})
    print(f"\n{out['answer']}\n")
    if show_sql:
        print(f"[route: {out['route']} | status: {out['status']} | attempts: {out.get('attempts', 0)}]")
        if out.get("sql"):
            print(out["sql"], "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ride-sharing AI data agent")
    parser.add_argument("question", nargs="*", help="your question (leave empty for interactive mode)")
    parser.add_argument("--show-sql", action="store_true", help="print the route, status and final SQL")
    args = parser.parse_args()

    agent = build_data_agent()
    if args.question:
        ask(agent, " ".join(args.question), args.show_sql)
        return
    print("Ride-sharing data agent. Ask a question, or press Enter to quit.")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        ask(agent, q, args.show_sql)


if __name__ == "__main__":
    sys.exit(main())
