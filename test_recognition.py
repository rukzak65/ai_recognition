import json
import os
import time
from pathlib import Path

import psycopg
import pytest
import requests
import yaml


CASE = "2ndfl"
OPERATION = "POST /integration/accompany/v1/ai-agent/recognition-result"
RESULT_TIMEOUT = 60
POLL_INTERVAL = 10


def test_agent_recognizes_document():
    host = os.environ["API_HOST"].rstrip("/")
    pdf_path = Path("Docs/input") / f"{CASE}.pdf"
    expected_path = Path("Docs/expected") / f"{CASE}.json"

    with expected_path.open(encoding="utf-8") as file:
        expected_fields = json.load(file)
    if not isinstance(expected_fields, dict) or not expected_fields:
        pytest.fail(f"Укажи поля для проверки в {expected_path}")

    with open("db.local.yaml", encoding="utf-8") as file:
        db_config = yaml.safe_load(file)

    with psycopg.connect(
        db_config["dsn"],
        password=db_config["password"],
        connect_timeout=5,
        autocommit=True,
    ) as db:
        # Шаг 1. Запоминаем время по часам БД и загружаем файлы.
        with open("request.json", "rb") as metadata, pdf_path.open("rb") as pdf:
            sent_at = db.execute("SELECT clock_timestamp()").fetchone()[0]
            print(f"1. Запомнили время перед отправкой по часам БД: {sent_at}", flush=True)
            response = requests.post(
                f"{host}/accompany/v1/document/upload",
                files={
                    "request": ("request.json", metadata, "application/json"),
                    "files": (pdf_path.name, pdf, "application/pdf"),
                },
                cert=("tls.pem", "tls.key"),
                verify=False,
                timeout=30,
                allow_redirects=False,
            )

        assert response.status_code == 200, (
            f"Загрузка вернула HTTP {response.status_code}, ожидался HTTP 200"
        )
        print(f"2. Отправили запрос, получили HTTP {response.status_code}. Ждём результат от агента", flush=True)

        # Шаг 2. Ждём входящую запись о распознавании.
        deadline = time.monotonic() + RESULT_TIMEOUT
        while time.monotonic() < deadline:
            row = db.execute(
                """
                SELECT created_at, payload::jsonb
                FROM accompany_service.integration_log
                WHERE created_at >= %s
                  AND operation = %s
                ORDER BY created_at
                LIMIT 1
                """,
                (sent_at, OPERATION),
            ).fetchone()
            if row is not None:
                break

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_INTERVAL, remaining))
        else:
            pytest.fail(f"За {RESULT_TIMEOUT} секунд в integration_log не появился результат")

    created_at, payload = row
    for field_path, expected in expected_fields.items():
        full_path = f"request.content.prd.{field_path}"
        actual = payload
        for key in full_path.split("."):
            if not isinstance(actual, dict) or key not in actual:
                pytest.fail(f"В результате от {created_at} отсутствует поле {full_path}")
            actual = actual[key]

        if type(actual) is not type(expected) or actual != expected:
            pytest.fail(
                f"В результате от {created_at} поле {full_path} отличается от эталона"
            )
