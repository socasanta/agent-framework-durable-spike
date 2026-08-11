FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir --pre \
    "agent-framework-durabletask==1.0.0b260709" \
    "azure-identity>=1.23.0,<2"

COPY spike.py ./

USER 65532:65532

ENTRYPOINT ["python", "spike.py"]
CMD ["worker"]
