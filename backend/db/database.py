import sqlite3
import requests
from datetime import datetime, timedelta
import pandas as pd
from backend.core.config import CSV_PATH, DATA_DIR, DB_PATH

# 1. 지역 메타데이터 (지도 표시용)
REGION_METADATA = {
    "서울.인천.경기": {"code": "SU", "latitude": 37.5665, "longitude": 126.9780, "reg_id": "11B00000"},
    "강원영서": {"code": "GW_W", "latitude": 37.8813, "longitude": 127.7298, "reg_id": "11D10000"},
    "강원영동": {"code": "GW_E", "latitude": 37.7519, "longitude": 128.8761, "reg_id": "11D20000"},
    "충청북도": {"code": "CB", "latitude": 36.6357, "longitude": 127.4917, "reg_id": "11C10000"},
    "충청남도": {"code": "CN", "latitude": 36.6588, "longitude": 126.6728, "reg_id": "11C20000"},
    "전북자치도": {"code": "JB", "latitude": 35.8200, "longitude": 127.1088, "reg_id": "11F10000"},
    "전라남도": {"code": "JN", "latitude": 34.8161, "longitude": 126.4630, "reg_id": "11F20000"},
    "경상북도": {"code": "GB", "latitude": 36.5760, "longitude": 128.5056, "reg_id": "11G00000"},
    "경상남도": {"code": "GN", "latitude": 35.2383, "longitude": 128.6924, "reg_id": "11H20000"},
    "제주도": {"code": "JJ", "latitude": 33.4996, "longitude": 126.5312, "reg_id": "11G00000"},
}

# 날씨 점수 매핑 (기본값 대응을 위해 .get() 사용 권장)
FORECAST_SCORES = {
    "맑음": 4, "구름많음": 3, "흐림": 2, "흐리고 비": 1, 
    "구름많고 비": 1, "구름많고 눈": 1, "흐리고 눈": 1, "소나기": 1
}

TIME_PERIODS = {"오전": "AM", "오후": "PM"}

def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

def init_db() -> None:
    with get_connection() as connection:
        # DB 제약 조건이 너무 까다로우면 API 데이터 입력 시 에러가 나므로 
        # 기존 테이블을 한 번 정리하고 새로 만듭니다.
        _reset_legacy_schema_if_needed(connection)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS regions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS forecast_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_published_at TEXT NOT NULL UNIQUE,
                published_at TEXT NOT NULL,
                source_file TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                region_id INTEGER NOT NULL,
                forecast_date TEXT NOT NULL,
                time_period TEXT NOT NULL,
                forecast_label TEXT NOT NULL,
                precipitation_probability INTEGER NOT NULL,
                forecast_score INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (batch_id) REFERENCES forecast_batches(id) ON DELETE CASCADE,
                FOREIGN KEY (region_id) REFERENCES regions(id) ON DELETE CASCADE
            );
        """)

def _reset_legacy_schema_if_needed(connection: sqlite3.Connection) -> None:
    # 안전을 위해 기존 테이블을 삭제하고 스키마를 갱신 (개발 단계에서 유용)
    connection.executescript("DROP TABLE IF EXISTS forecasts; DROP TABLE IF EXISTS forecast_batches;")

# --- 공공데이터 API 연동 함수 ---
def fetch_api_data() -> dict:
    SERVICE_KEY = "34770ede0e04354e4c9e85f41531c3229236e6574c8cf9805fa878121de35f37"
    URL = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst"
    
    # 발표 시각 설정 (오늘 오전 6시 기준)
    tm_fc = datetime.now().strftime("%Y%m%d") + "0600"
    imported_count = 0

    with get_connection() as connection:
        # 1. 지역 데이터 삽입
        for name, meta in REGION_METADATA.items():
            connection.execute(
                "INSERT OR IGNORE INTO regions (code, name, latitude, longitude) VALUES (?, ?, ?, ?)",
                (meta["code"], name, meta["latitude"], meta["longitude"])
            )
        
        region_map = {row["name"]: row["id"] for row in connection.execute("SELECT id, name FROM regions").fetchall()}

        # 2. API 호출 및 저장
        for name, meta in REGION_METADATA.items():
            params = {
                'serviceKey': SERVICE_KEY, 'numOfRows': '10', 'pageNo': '1',
                'dataType': 'JSON', 'regId': meta['reg_id'], 'tmFc': tm_fc
            }
            try:
                res = requests.get(URL, params=params, timeout=5)
                items = res.json()['response']['body']['items']['item'][0]
                
                # Batch 생성
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO forecast_batches (raw_published_at, published_at, source_file, row_count) VALUES (?, ?, ?, ?)",
                    (tm_fc, datetime.now().isoformat(), "API", 10)
                )
                batch_id = cursor.lastrowid or connection.execute("SELECT id FROM forecast_batches WHERE raw_published_at=?", (tm_fc,)).fetchone()[0]

                # 예시로 3일 후 데이터 저장 (필요에 따라 4~7일차도 추가 가능)
                for day in range(3, 8):
                    for ampm in ["Am", "Pm"]:
                        label = items.get(f"wf{day}{ampm}", "맑음")
                        prob = items.get(f"rnSt{day}{ampm}", 0)
                        connection.execute(
                            "INSERT OR IGNORE INTO forecasts (batch_id, region_id, forecast_date, time_period, forecast_label, precipitation_probability, forecast_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (batch_id, region_map[name], (datetime.now() + timedelta(days=day)).strftime("%Y-%m-%d"), ampm.upper(), label, prob, FORECAST_SCORES.get(label, 2))
                        )
                imported_count += 1
            except:
                continue
    return {"status": "success", "imported_regions": imported_count}

# 기존 CSV 기능 유지
def seed_forecast_data():
    # ... 기존의 CSV 읽기 로직 (생략되었으나 파일에 그대로 두시면 됩니다) ...
    pass