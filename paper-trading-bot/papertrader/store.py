"""Persistenza su disco: stato del conto (JSON) e registri (JSON Lines)."""

from __future__ import annotations

import json
import os
from pathlib import Path


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.jsonl"
        self.equity_path = self.dir / "equity.jsonl"
        self.journal_path = self.dir / "journal.jsonl"

    def exists(self) -> bool:
        return self.state_path.exists()

    def load_state(self) -> dict:
        if not self.exists():
            raise FileNotFoundError(
                f"Nessun conto trovato in {self.dir}. Esegui prima: python -m papertrader init"
            )
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def append(self, path: Path, record: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def reset(self) -> None:
        for p in (self.state_path, self.trades_path, self.equity_path, self.journal_path):
            if p.exists():
                p.unlink()
