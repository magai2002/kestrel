"""Cheap regression check on the committed, already-executed notebook:
no cell has an error output. Doesn't re-execute the notebook (CI has no
fetched market data to run it against until Phase 6's committed demo
sample exists) -- just catches someone committing a broken run.
"""

import json
from pathlib import Path


def test_first_look_notebook_has_no_error_outputs() -> None:
    path = Path("notebooks/01_first_look.ipynb")
    notebook = json.loads(path.read_text())
    errors = []
    for i, cell in enumerate(notebook["cells"]):
        for output in cell.get("outputs", []):
            if output.get("output_type") == "error":
                errors.append((i, output.get("ename"), output.get("evalue")))
    assert errors == [], f"notebook has error outputs: {errors}"


def test_first_look_notebook_has_executed_code_cells() -> None:
    path = Path("notebooks/01_first_look.ipynb")
    notebook = json.loads(path.read_text())
    code_cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert len(code_cells) > 0
    assert all(c.get("execution_count") is not None for c in code_cells), (
        "every code cell should have been executed before committing"
    )
