# TCP 설정 프로토콜 / HTTP API

Gateway는 PySide6 설정 클라이언트와 웹 화면을 위해 TCP 제어 서버와 HTTP API를 제공합니다. 현재 버전은 인증 토큰을 요구하지 않습니다. 설비 내부망, 전용망, VPN 환경에서 사용하는 것을 전제로 합니다.

## TCP 전송 규칙

- 기본 수신 주소: `0.0.0.0:15150` (기존 설정 파일의 포트는 유지)
- UTF-8 JSON 객체 1개를 한 줄로 전송합니다.
- 각 메시지 끝에는 LF(`\n`)가 필요합니다.
- 같은 TCP 연결에서 여러 요청을 연속 처리할 수 있습니다.
- 요청 필드는 `id`, `command`, `params`입니다. `token` 필드는 보내도 무시됩니다.

요청 예:

```json
{"id":"req-1","command":"get_status","params":{}}
```

성공 응답:

```json
{"id":"req-1","ok":true,"result":{"plc_state":"connected"}}
```

실패 응답:

```json
{"id":"req-1","ok":false,"error":"bad_request","message":"설명"}
```

## 명령 목록

| command | params | 기능 |
|---|---|---|
| `ping` | `{}` | 연결 확인 |
| `get_status` | `{}` | PLC 상태, 카운터, 오류, 공유 메모리 상태 조회 |
| `get_config` | `{}` | 현재 설정 조회 |
| `update_config` | `{"patch": {...}}` | 부분 설정 검증 후 저장 및 즉시 적용 |
| `set_web_enabled` | `{"enabled": true}` | 웹 서버 켜기/끄기 |
| `set_plc_enabled` | `{"enabled": true}` | PLC 통신 켜기/끄기 |
| `reconnect_plc` | `{}` | 현재 소켓을 닫고 즉시 재연결 |
| `get_shared_memory` | `{}` | 이름, Offset, 길이, sequence/ACK 조회 |
| `read_shared_memory` | `{"area":"read"}` | `read` 또는 `write` 영역을 HEX로 조회 |
| `write_shared_memory` | `{"hex":"00 01 ..."}` | TX 영역에 정확한 길이의 데이터를 커밋 |

부분 설정 변경 예:

```json
{
  "id": "req-2",
  "command": "update_config",
  "params": {
    "patch": {
      "plc": {
        "host": "192.168.0.20",
        "port": 2004,
        "retry_initial_ms": 500,
        "retry_max_ms": 10000
      },
      "read": {
        "address": "D1000",
        "byte_count": 100,
        "interval_ms": 100
      }
    }
  }
}
```

PLC, 읽기, 쓰기, 공유 메모리 변경은 즉시 worker에 반영됩니다. `control`의 bind 주소/Port 또는 `logging` 설정 변경은 응답의 `restart_required`에 표시되며 Gateway 재시작 후 적용됩니다.

## HTTP API

웹 화면과 같은 API입니다. 인증 헤더는 필요 없습니다.

| Method | 경로 | Body / 기능 |
|---|---|---|
| `GET` | `/api/status` | 상태 조회 |
| `GET` | `/api/config` | 설정 조회 |
| `PUT` | `/api/config` | `{"patch": {...}}` |
| `POST` | `/api/plc/reconnect` | `{}` |
| `POST` | `/api/plc/enabled` | `{"enabled":true}` |
| `POST` | `/api/web/enabled` | `{"enabled":false}` |
| `GET` | `/api/shm` | 공유 메모리 메타데이터 |
| `POST` | `/api/shm/read` | `{"area":"read"}` |
| `POST` | `/api/shm/write` | `{"hex":"..."}` |

## 보안 메모

TCP/HTTP 모두 TLS와 인증을 제공하지 않습니다. 외부망에 직접 노출하지 말고, 방화벽으로 PC 클라이언트와 필요한 관리 PC만 접근하도록 제한하세요. 외부 접속이 필요하면 VPN 또는 TLS reverse proxy 뒤에 배치하는 구성을 권장합니다.
