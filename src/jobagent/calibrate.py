from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from jobagent.config import StrictModel, read_yaml
from jobagent.jd import MatchError
from jobagent.llm import LLMError
from jobagent.matcher import Matcher


class LabelItem(StrictModel):
    file: str
    label: Literal["apply", "watch_later", "reject"]
    note: str = ""


class LabelFile(StrictModel):
    jobs: list[LabelItem] = []


@dataclass(frozen=True)
class Row:
    file: str
    label: str
    decision: Optional[str] = None
    score: Optional[float] = None
    reason: str = ""
    error: str = ""

    @property
    def agrees(self) -> bool:
        return self.decision == self.label


def load_labels(path: Path) -> LabelFile:
    return LabelFile.model_validate(read_yaml(Path(path)))


def run_calibration(matcher: Matcher, labels: LabelFile, base_dir: Path) -> list[Row]:
    rows = []
    for item in labels.jobs:
        try:
            result = matcher.evaluate((Path(base_dir) / item.file).read_text(encoding="utf-8"))
        except (OSError, MatchError, LLMError) as error:
            rows.append(Row(item.file, item.label, error=str(error)))
            continue
        rows.append(Row(item.file, item.label, result.decision, result.score, result.reason))
    return rows
