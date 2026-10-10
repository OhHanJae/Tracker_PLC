# Windows / Linux 운용

## 공통 요구사항

- Python 3.10 이상
- PLC와 Gateway 사이 TCP 통신 허용
- PLC Ethernet 모듈에서 XGT 전용 서버가 활성화되어 있고 IP/Port가 설정과 일치할 것
- 설정된 읽기/쓰기 범위(기본 각각 200바이트)가 실제 PLC 디바이스 범위를 넘지 않을 것

## Windows

개발/시험은 `run_gateway.bat`으로 충분합니다. 상시 운용은 Windows 작업 스케줄러에 아래 항목을 등록하는 방식을 권장합니다.

1. 트리거: 시스템 시작 시
2. 프로그램: 설치된 Python 3.10+ `python.exe`의 절대 경로 (별도 `.venv`를 만든 경우 그 인터프리터)
3. 인수: `gateway.py --config config.json`
4. 시작 위치: 프로젝트 폴더 절대 경로
5. 실패 시 1분 뒤 재시작 설정

설정 앱은 `run_config_client.bat`으로 실행합니다. Gateway와 다른 PC에서 실행해도 됩니다.

## Ubuntu / Debian

먼저 한 번 실행해 `.venv-linux`와 `config.json`을 만듭니다. RDK X5 3.5 이미지에서도 Python 3.10 이상과 `python3-venv`가 필요하며 Gateway 서비스에는 GUI가 필요하지 않습니다.

```bash
chmod +x run_gateway.sh
./run_gateway.sh
```

### systemd 예

프로젝트를 `/opt/xgt-gateway`에 설치했다고 가정합니다.

```ini
[Unit]
Description=XGT Shared Memory Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xgtgateway
Group=xgtgateway
WorkingDirectory=/opt/xgt-gateway
ExecStart=/opt/xgt-gateway/.venv-linux/bin/python /opt/xgt-gateway/gateway.py --config /opt/xgt-gateway/config.json
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

설치 후:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xgt-gateway.service
sudo systemctl status xgt-gateway.service
```

`User`, `Group`, 경로는 실제 환경에 맞게 바꿉니다. `config.json`과 로그 폴더에 해당 사용자의 읽기/쓰기 권한이 필요합니다. Core와 Gateway는 동일 호스트의 동일 Linux 사용자로 실행해 POSIX 공유 메모리를 읽고 쓸 수 있어야 합니다. Windows에서도 같은 사용자 세션으로 실행합니다.

## 네트워크 점검

Gateway PC에서 PLC Port 확인:

```bash
nc -vz 192.168.0.10 2004
```

설정 PC에서 Gateway TCP 설정 서버 확인:

```bash
nc -vz GATEWAY_IP 15150
```

연결은 되는데 읽기 NAK가 발생하면 다음 순서로 확인합니다.

1. XGT 서버/클라이언트 설정과 Port
2. CPU 계열(`cpu_info`, 기본 XGK `0xA0`)
3. Base/Slot 값
4. 연속 BYTE 주소 환산: `D1000` → `%DB2000`
5. 요청 범위: 시작 주소부터 설정된 바이트 수(기본 200바이트)가 유효한지
6. PLC 운전 상태와 Ethernet 모듈 진단 로그

## 로그

기본 로그는 `logs/xgt_gateway.log`에 저장됩니다. 2MB마다 회전하고 최대 5개 백업을 유지합니다. 로그에는 TCP 연결/재연결 및 XGT 오류가 기록되지만 PLC 데이터 본문은 기록하지 않습니다.
