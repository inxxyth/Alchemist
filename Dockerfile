FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py llm_client.py sis_adapter.py gibbon_bridge.py start_msoe.py \
     gibbon_bootstrap_settings.py gibbon_seed_demo_ops.py gibbon_install_msoe_module.py \
     roi_model.py build_knowledge_graph.py ./
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
