FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py llm_client.py sis_adapter.py gibbon_bridge.py start_msoe.py \
     gibbon_bootstrap_settings.py gibbon_seed_demo_ops.py gibbon_install_msoe_module.py \
     roi_model.py build_knowledge_graph.py schema.sql \
     policy_pack.py validate_policy_pack.py \
     audit_log.py retention_sweep.py residency_check.py ./
# Connector framework (Phase 2) — per-dialect field maps + registry.
COPY connectors/ ./connectors/
# Policy-pack library (Phase 5) — the reference pack + format spec, and the
# synthetic fixture packs used by test_policy_pack.py.  policy_pack.py is
# required at import time by sis_adapter; policy_vault/ is the default pack
# resolved by MSOE_POLICY_PACK.
COPY policy_vault/ ./policy_vault/
COPY fixtures/ ./fixtures/
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
