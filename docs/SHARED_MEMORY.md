# 공유 메모리 레이아웃

Gateway는 Python `multiprocessing.shared_memory`를 사용합니다. 기본 이름은 `xgt_gateway_v1`, 기본 크기는 512바이트입니다.

## 고정 64바이트 Header

모든 다중 바이트 수치는 little-endian입니다.

| Offset | 크기 | 형 | 필드 | 설명 |
|---:|---:|---|---|---|
| 0 | 4 | `char[4]` | magic | ASCII `XGSM` |
| 4 | 2 | `uint16` | layout_version | 현재 1 |
| 6 | 2 | `uint16` | header_size | 64 |
| 8 | 4 | `uint32` | total_size | 전체 공유 메모리 크기 |
| 12 | 4 | `uint32` | read_offset | RX 데이터 시작 위치 |
| 16 | 4 | `uint32` | read_length | RX 데이터 길이 |
| 20 | 4 | `uint32` | write_offset | TX 데이터 시작 위치 |
| 24 | 4 | `uint32` | write_length | TX 데이터 길이 |
| 28 | 4 | `uint32` | read_sequence | PLC 읽기 데이터 sequence |
| 32 | 4 | `uint32` | write_sequence | 외부 앱 쓰기 커밋 sequence |
| 36 | 4 | `uint32` | write_ack_sequence | PLC 쓰기 ACK 완료 sequence |
| 40 | 4 | `uint32` | status_flags | 연결/최근 처리 상태 |
| 44 | 4 | `int32` | last_error_code | 0 정상, -1 TCP/일반 오류, 양수 XGT 오류 |
| 48 | 8 | `uint64` | last_read_time_ns | Unix epoch nanoseconds |
| 56 | 8 | `uint64` | last_write_time_ns | Unix epoch nanoseconds |

기본 데이터 배치는 다음과 같습니다.

| 영역 | Offset | 길이 |
|---|---:|---:|
| Header | 0 | 64 |
| PLC → 앱 RX | 64 | 100 |
| 예약 여유 | 164 | 92 |
| 앱 → PLC TX | 256 | 100 |
| 예약 여유 | 356 | 156 |

설정 변경 시 RX/TX가 겹치거나 전체 크기를 벗어나면 저장을 거부합니다.

## status_flags

| Bit | Mask | 의미 |
|---:|---:|---|
| 0 | `0x00000001` | PLC TCP 연결됨 |
| 1 | `0x00000002` | 최근 PLC 읽기 성공 |
| 2 | `0x00000004` | 최근 PLC 쓰기 성공 |

## 일관된 RX 읽기

Gateway는 데이터를 복사하는 동안 `read_sequence`를 홀수로 두고, 완료 후 짝수로 변경합니다.

1. `read_sequence`를 읽습니다.
2. 홀수면 잠시 후 다시 시도합니다.
3. RX 데이터 전체를 복사합니다.
4. `read_sequence`를 다시 읽습니다.
5. 두 값이 같고 짝수일 때만 데이터를 사용합니다.

이 방법은 복사 도중의 절반짜리 데이터를 읽는 것을 방지합니다.

## 안전한 TX 쓰기와 ACK

외부 앱도 같은 순서를 지켜야 합니다.

1. 현재 `write_sequence`에서 다음 홀수 값을 기록합니다.
2. TX 데이터 전체를 복사합니다.
3. 다음 짝수 값을 `write_sequence`에 기록해 커밋합니다.
4. Gateway가 PLC 쓰기 ACK를 받으면 `write_ack_sequence`에 그 짝수 값을 기록합니다.

`write_sequence == write_ack_sequence`이고 짝수라면 해당 데이터의 PLC 쓰기가 완료된 상태입니다. sequence는 `uint32`이며 오버플로 후 0으로 돌아갑니다.

## Python 사용 예

프로젝트의 `src`를 import 경로에 둔 상태에서:

```python
from xgt_gateway.shared_memory_bridge import SharedMemoryClient

payload = bytes([0x12, 0x34]) + bytes(98)

with SharedMemoryClient("xgt_gateway_v1") as shm:
    latest_plc_data = shm.read_plc_data()
    committed = shm.write_plc_data(payload)
    print("TX sequence:", committed)
    print("ACK:", shm.write_acknowledged())
```

실행 가능한 전체 예제는 다음 파일에 있습니다.

- `examples/shared_memory_reader.py`
- `examples/shared_memory_writer.py`

## C/C++ 구조체 정의

```cpp
#pragma pack(push, 1)
struct XgtSharedHeader {
    char magic[4];
    std::uint16_t layoutVersion;
    std::uint16_t headerSize;
    std::uint32_t totalSize;
    std::uint32_t readOffset;
    std::uint32_t readLength;
    std::uint32_t writeOffset;
    std::uint32_t writeLength;
    std::uint32_t readSequence;
    std::uint32_t writeSequence;
    std::uint32_t writeAckSequence;
    std::uint32_t statusFlags;
    std::int32_t lastErrorCode;
    std::uint64_t lastReadTimeNs;
    std::uint64_t lastWriteTimeNs;
};
#pragma pack(pop)

static_assert(sizeof(XgtSharedHeader) == 64);
```

Windows는 `OpenFileMapping`/`MapViewOfFile`, Linux는 `shm_open`/`mmap`으로 연결할 수 있습니다. Linux POSIX 이름에는 API 호출 시 `/xgt_gateway_v1`처럼 선행 `/`가 필요할 수 있습니다. sequence 읽기/쓰기는 4바이트 정렬 상태에서 acquire/release 메모리 순서를 사용하세요.

## 운용 주의

- 공유 메모리 이름 또는 레이아웃을 바꾸면 기존 매핑을 잡고 있는 외부 앱도 다시 연결해야 합니다.
- Linux에서 비정상 종료 후 같은 이름의 `/dev/shm` 객체가 남고 레이아웃이 다르면 Gateway는 안전을 위해 시작을 거부합니다. 기존 프로세스가 없는지 확인한 후 해당 객체를 정리하거나 새 이름을 사용하세요.
- `unlink_on_exit=true`이면 정상 종료 시 이름을 제거합니다. 이미 연결된 프로세스의 매핑은 OS 규칙에 따라 닫힐 때까지 유지될 수 있습니다.

