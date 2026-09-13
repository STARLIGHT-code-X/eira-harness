"""Deterministic fixture provider for a real, offline tool-loop demonstration."""
import json
import math
from datetime import date, timedelta


def sample_csv() -> str:
    rows = ["date,close"]
    for i in range(180):
        stamp = date(2025, 1, 1) + timedelta(days=i)
        value = 100 + i * .09 + math.sin(i / 8) * 9 + math.sin(i / 2.3) * 1.2
        rows.append(f"{stamp.isoformat()},{value:.4f}")
    return "\n".join(rows) + "\n"


class DemoProvider:
    model = "offline-scripted-demo"

    def __init__(self):
        self.step = 0

    def complete(self, messages, tools):
        self.step += 1
        if self.step == 1:
            return self.call("set_plan", {"plan": "1. Inspect synthetic data.\n2. Backtest SMA 5/20 with costs.\n3. Report verified metrics."})
        if self.step == 2:
            return self.call("read_file", {"path": "synthetic.csv"})
        if self.step == 3:
            return self.call("backtest_sma", {"path": "synthetic.csv", "fast": 5, "slow": 20,
                                             "fee_bps": 10, "slippage_bps": 5, "periods_per_year": 365})
        result = json.loads(messages[-1]["content"])
        if not result["ok"]:
            answer = "Demo tool failed: " + result["error"]
        else:
            metrics = result["result"]["metrics"]
            answer = (f"Synthetic-data backtest complete. SMA 5/20 returned {metrics['total_return']:.2%}; "
                      f"maximum drawdown {metrics['max_drawdown']:.2%}; "
                      f"{metrics['round_trips']} round trips after fees and slippage.\n"
                      "This is a scripted offline demonstration using synthetic prices, not an LLM response or live market result.\n"
                      "The session history and tool trace are saved locally.")
        return {"role": "assistant", "content": answer}, {}

    def call(self, name, arguments):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"demo_{self.step}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}, {}
