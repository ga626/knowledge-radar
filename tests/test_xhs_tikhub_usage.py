from runtime import env_loader
from runtime import xhs_tikhub_fallback as paid
from runtime.xhs_tikhub_usage import tikhub_usage_summary

def test_paid_fallback_enabled_without_precise_price_config(monkeypatch,tmp_path):
    monkeypatch.delenv("KR_PAID_POLICY_PATH",raising=False)
    monkeypatch.setenv("KR_PAID_USAGE_PATH",str(tmp_path/"usage.json"))
    monkeypatch.delenv("TIKHUB_API_KEY",raising=False)
    assert tikhub_usage_summary()["enabled"]
    assert paid.execute_paid_operation("search",{"keyword":"q"},task_id="r1",trigger="AUTH_REQUIRED")["reason_code"]=="TIKHUB_API_KEY_MISSING"


def test_runtime_env_loader_reads_repo_dotenv(monkeypatch,tmp_path):
    monkeypatch.delenv("KR_RUNTIME_ENV_PATH",raising=False)
    monkeypatch.delenv("TIKHUB_API_KEY",raising=False)
    monkeypatch.setattr(env_loader,"REPO_ROOT",str(tmp_path))
    (tmp_path/".env").write_text("TIKHUB_API_KEY=fixture\n",encoding="utf-8")
    assert env_loader.load_runtime_env()==str(tmp_path/".env")
    assert paid._api_key()=="fixture"
