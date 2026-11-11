"""Phase 3 evaluation: trajectory grader + dataset runner + CI gate (python -m log_on.evals)."""
from .grader import WEIGHTS, check_thresholds, grade, step_order, summarise, tool_selection  # noqa: F401
