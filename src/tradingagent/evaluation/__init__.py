"""The evaluation lab — does the research actually work?

BUILD_PLAN.md ends on the only claim this project is allowed to make: "the
journal is the only benchmark that counts". The journal as shipped through M6
records what was recommended. It cannot answer whether the recommendations were
any good, and it cannot answer the sharper question underneath — whether the
parts we keep paying for (the debate, the signal layer, the deep tier) earn
their tokens against a cheaper alternative.

Four things were missing, and this package adds them:

- :mod:`.provenance` — a run that cannot name its own inputs cannot be graded.
  Every record carries the run id, the snapshot, the git commit, a hash of the
  behaviour-changing configuration, the model ids, the prompt hashes and the
  universe version.
- :mod:`.ledger` — an append-only experiment ledger beside the journal, holding
  the *whole* pre-selection pool rather than the shortlist, so the names the
  screener rejected are evidence too.
- :mod:`.outcomes` — resolution at maturity: returns at 1/5/10/20/60 trading
  days, excess against SPY and the sector ETF, MFE and MAE, and whether the
  published entry ever triggered.
- :mod:`.grading` and :mod:`.report` — signal grading v2 over those outcomes,
  and the weekly write-up that says "insufficient data" wherever the sample
  cannot support a conclusion.

Nothing here spends a token or places an order. It reads records the daily run
already wrote and prices them against bars.
"""

from .ledger import (
    CandidateRecord,
    DecisionRecord,
    ExperimentLedger,
    OutcomeRecord,
    RunRecord,
)
from .provenance import Provenance, build_provenance

__all__ = [
    "CandidateRecord",
    "DecisionRecord",
    "ExperimentLedger",
    "OutcomeRecord",
    "Provenance",
    "RunRecord",
    "build_provenance",
]
