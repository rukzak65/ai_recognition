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
PRD_PREFIX = "request.content.prd."


def iter_expected_fields(data, prefix=""):
    for key, value in data.items():
        path = prefix + key
        if isinstance(value, dict) and value:
            yield from iter_expected_fields(value, path + ".")
        else:
            yield path, value


def iter_differences(actual, expected, path=""):
    if type(actual) is not type(expected):
        yield f"{path or 'payload'} — значение или тип отличается от эталона"
    elif isinstance(expected, dict):
        for key, value in expected.items():
            field = f"{path}.{key}" if path else key
            if key in actual:
                yield from iter_differences(actual[key], value, field)
            else:
                yield f"{field} — поле отсутствует"
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            yield f"{path} — ожидалось элементов: {len(expected)}, получено: {len(actual)}"
        for index, (item, expected_item) in enumerate(zip(actual, expected)):
            yield from iter_differences(item, expected_item, f"{path}[{index}]")
    elif actual != expected:
        yield f"{path} — значение или тип отличается от эталона"


def test_agent_recognizes_document():
    host = os.environ["API_HOST"].rstrip("/")
    pdf_path = Path("Docs/input") / f"{CASE}.pdf"
    expected_path = Path("Docs/expected") / f"{CASE}.json"

    with expected_path.open(encoding="utf-8") as file:
        expected = json.load(file)
    if not isinstance(expected, dict) or not expected:
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
    if "request" in expected or "response" in expected:
        differences = list(iter_differences(payload, expected))
    else:
        differences = []
        for path, value in iter_expected_fields(expected):
            if path != "status" and not path.startswith(("request.", "response.")):
                path = PRD_PREFIX + path
            actual = payload
            for key in path.split("."):
                if not isinstance(actual, dict) or key not in actual:
                    differences.append(f"{path} — поле отсутствует")
                    break
                actual = actual[key]
            else:
                differences.extend(iter_differences(actual, value, path))

    if differences:
        details = "\n".join(f"- {difference}" for difference in differences)
        pytest.fail(f"В результате от {created_at} найдено расхождений: {len(differences)}\n{details}")
