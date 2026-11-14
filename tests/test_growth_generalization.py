"""The real-provider run is explicit; ordinary CI validates its frozen coverage."""
import importlib.util
from pathlib import Path


def test_real_evaluation_has_nine_non_leaking_inputs_and_three_repetitions():
    path = Path(__file__).resolve().parents[1]/"scripts/evaluate_growth_model.py"
    spec = importlib.util.spec_from_file_location("growth_eval",path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0,str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    assert len(module.CASES)==9 and len(set(module.CASES))==9
    original_branches={branch for _,branch in module.CASES}
    assert set(module.FACTS)==original_branches | {module.MEMORY_CASE}
    assert len({product for product,_ in module.CASES})==3
    assert set(module.RUBRIC)==original_branches
    assert module.selected_cases()==[
        (product,branch,repetition)
        for product,branch in module.CASES
        for repetition in (1,2,3)
    ]
    assert module.MEMORY_CASE not in {branch for _,branch,_ in module.selected_cases()}
    assert module.selected_cases(module.MEMORY_CASE)==[("quote",module.MEMORY_CASE,1)]
    # Each three-run case has an identical logical product/feedback input;
    # model prompt generation never receives the evaluator rubric.
    import inspect
    implementation = inspect.getsource(module.evaluate)
    assert 'RUBRIC[branch]' not in implementation.split('record["result"] = await agent.decide')[0]
