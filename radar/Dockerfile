FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Europe/Rome
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY radar ./radar
USER 1000
CMD ["python", "-m", "radar"]
