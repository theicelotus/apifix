from src.suspense.api import load_prompts


def test_load_prompts(tmp_path):
    path = tmp_path / "prompts.yaml"
    path.write_text("rate: Rate the suspense\n")
    assert load_prompts(path) == {"rate": "Rate the suspense"}
