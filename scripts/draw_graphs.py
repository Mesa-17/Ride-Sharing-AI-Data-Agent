"""Write each agent graph as Mermaid (docs/*.mmd). GitHub renders Mermaid in Markdown directly.

    python scripts/draw_graphs.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.data_agent import build_data_agent      # noqa: E402
from agents.etl_analyst import build_etl_analyst    # noqa: E402
from agents.sql_analyst import build_sql_analyst    # noqa: E402

if __name__ == "__main__":
    stub = lambda level: None  # graphs are only drawn, never run
    graphs = {"data_agent": build_data_agent(llm=stub, db=object()),
              "sql_analyst": build_sql_analyst(llm=stub, db=object()),
              "etl_analyst": build_etl_analyst(llm=stub)}
    for name, graph in graphs.items():
        path = ROOT / "docs" / f"{name}.mmd"
        path.write_text(graph.get_graph().draw_mermaid(), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
