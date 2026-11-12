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
    assert {branch for _,branch in module.CASES}==set(module.FACTS)
    assert len({product for product,_ in module.CASES})==3
    assert set(module.RUBRIC)==set(module.FACTS)
    # Each three-run case has an identical logical product/feedback input;
    # model prompt generation never receives the evaluator rubric.
    import inspect
    implementation = inspect.getsource(module.evaluate)
    assert 'range(1,4)' in implementation
    assert 'RUBRIC[branch]' not in implementation.split('record["result"] = await agent.decide')[0]
